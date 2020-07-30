import logging
import os
from datetime import datetime, timezone
from logging.config import fileConfig
from subprocess import CalledProcessError

from botocore.config import Config
from configparser import ConfigParser
from retrying import retry

from common.schedulers.slurm_commands import get_nodes_info
from common.time_utils import seconds
from common.utils import check_command_output, sleep_remaining_loop_time
from slurm_plugin.common import CONFIG_FILE_DIR, TIMESTAMP_FORMAT, InstanceManager, log_exception, time_is_up

LOOP_TIME = 60
# Computemgtd config is under /opt/slurm/etc/pcluster; all compute nodes share a config
COMPUTEMGTD_CONFIG_PATH = "/opt/slurm/etc/pcluster/parallelcluster_computemgtd.conf"
log = logging.getLogger(__name__)


class ComputemgtdConfig:
    DEFAULTS = {
        # Basic configs
        "max_retry": 5,
        "loop_time": LOOP_TIME,
        "proxy": "NONE",
        "clustermgtd_timeout": 180,
        "disable_computemgtd_actions": False,
        "slurm_nodename_file": os.path.join(CONFIG_FILE_DIR, "slurm_nodename"),
        "logging_config": os.path.join(
            os.path.dirname(__file__), "logging", "parallelcluster_computemgtd_logging.conf"
        ),
    }

    def __init__(self, config_file_path):
        self._get_config(config_file_path)

    def __repr__(self):
        attrs = ", ".join(["{key}={value}".format(key=key, value=repr(value)) for key, value in self.__dict__.items()])
        return "{class_name}({attrs})".format(class_name=self.__class__.__name__, attrs=attrs)

    def _get_config(self, config_file_path):
        """Get computemgtd configuration."""
        log.info("Reading %s", config_file_path)
        config = ConfigParser()
        try:
            config.read_file(open(config_file_path, "r"))
        except IOError:
            log.error(f"Cannot read cluster manager configuration file: {config_file_path}")
            raise

        # Get config settings
        self.region = config.get("computemgtd", "region")
        self.cluster_name = config.get("computemgtd", "cluster_name")
        # Configure boto3 to retry 5 times by default
        self._boto3_config = {"retries": {"max_attempts": self.DEFAULTS.get("max_retry"), "mode": "standard"}}
        self.loop_time = config.getint("computemgtd", "loop_time", fallback=self.DEFAULTS.get("loop_time"))
        self.clustermgtd_timeout = config.getint(
            "computemgtd", "clustermgtd_timeout", fallback=self.DEFAULTS.get("clustermgtd_timeout"),
        )
        self.disable_computemgtd_actions = config.getboolean(
            "computemgtd", "disable_computemgtd_actions", fallback=self.DEFAULTS.get("disable_computemgtd_actions"),
        )
        self.clustermgtd_heartbeat_file_path = config.get("computemgtd", "clustermgtd_heartbeat_file_path")
        self.slurm_nodename_file = config.get(
            "computemgtd", "slurm_nodename_file", fallback=self.DEFAULTS.get("slurm_nodename_file")
        )

        proxy = config.get("computemgtd", "proxy", fallback=self.DEFAULTS.get("proxy"))
        if proxy != "NONE":
            self._boto3_config["proxies"] = {"https": proxy}
        self.boto3_config = Config(**self._boto3_config)
        self.logging_config = config.get("computemgtd", "logging_config", fallback=self.DEFAULTS.get("logging_config"))
        # Log configuration
        log.info(self.__repr__())


@log_exception(log, "self terminating compute instance", catch_exception=CalledProcessError, raise_on_error=False)
def _self_terminate(computemgtd_config):
    """Self terminate the instance."""
    instance_manager = InstanceManager(
        computemgtd_config.region, computemgtd_config.cluster_name, computemgtd_config.boto3_config
    )
    self_instance_id = check_command_output("curl -s http://169.254.169.254/latest/meta-data/instance-id")
    log.info("Self terminating instance %s now!", self_instance_id)
    instance_manager.delete_instances([self_instance_id], terminate_batch_size=1)


def _get_clustermgtd_heartbeat(clustermgtd_heartbeat_file_path):
    """Get clustermgtd's last heartbeat."""
    with open(clustermgtd_heartbeat_file_path, "r") as timestamp_file:
        # Note: heartbeat must be written with datetime.strftime to convert localized datetime into str
        # datetime.strptime will not work with str(datetime)
        # Example timestamp written to heartbeat file: 2020-07-30 19:34:02.613338+00:00
        return datetime.strptime(timestamp_file.read(), TIMESTAMP_FORMAT)


def _expired_clustermgtd_heartbeat(last_heartbeat, current_time, clustermgtd_timeout):
    """Test if clustermgtd heartbeat is expired."""
    if time_is_up(last_heartbeat, current_time, clustermgtd_timeout):
        log.error(
            "Clustermgtd has been offline since %s. Current time is %s. Timeout of %s seconds has expired!",
            last_heartbeat,
            current_time,
            clustermgtd_timeout,
        )
        return True
    return False


def _is_self_node_down(slurm_nodename_file):
    """
    Check if self node is down in slurm.

    This check prevents termination of a node that is still well-attached to the scheduler.
    Note: a node that is not attached to the scheduler will be put into DOWN* automatically after slurmd.
    """
    try:
        log.info("Reading self nodename from %s", slurm_nodename_file)
        with open(slurm_nodename_file, "r") as nodename_file:
            self_nodename = nodename_file.read()
        self_node = get_nodes_info(self_nodename)[0]
        log.info("Self node state is currently %s", self_node.__repr__())
        if self_node.is_down():
            return True
        return False
    except IOError as e:
        log.error(
            "Unable to read self nodename from %s with exception: %s\nConsidering node as down!", slurm_nodename_file, e
        )
    except Exception as e:
        # This could happen is slurmctld is down completely
        log.error("Unable to retrieve current node state from slurm with exception: %s\nConsidering node as down!", e)

    return True


def _fail_self_check(last_heartbeat, current_time, computemgtd_config):
    """Determine if self checks are failing and if the node should self-terminate."""
    return _expired_clustermgtd_heartbeat(
        last_heartbeat, current_time, computemgtd_config.clustermgtd_timeout
    ) and _is_self_node_down(computemgtd_config.slurm_nodename_file)


def _run_computemgtd():
    """Run computemgtd actions."""
    # Initial default heartbeat time as computemgtd startup time
    last_heartbeat = datetime.now(tz=timezone.utc)
    log.info("Initializing clustermgtd heartbeat to be computemgtd startup time: %s", last_heartbeat)
    while True:
        # Get current time
        current_time = datetime.now(tz=timezone.utc)
        # Get program config
        computemgtd_config = ComputemgtdConfig(os.path.join(COMPUTEMGTD_CONFIG_PATH))
        # Configure root logger
        try:
            fileConfig(computemgtd_config.logging_config, disable_existing_loggers=False)
        except Exception as e:
            log.warning(
                "Unable to configure logging from %s, using default logging settings.\nException: %s",
                computemgtd_config.logging_config,
                e,
            )
        # Check heartbeat
        try:
            last_heartbeat = _get_clustermgtd_heartbeat(computemgtd_config.clustermgtd_heartbeat_file_path)
            log.info("Latest heartbeat from clustermgtd: %s", last_heartbeat)
        except Exception as e:
            log.error("Unable to retrieve clustermgtd heartbeat with exception: %s", e)
        finally:
            if computemgtd_config.disable_computemgtd_actions:
                log.info("All computemgtd actions currently disabled")
            elif _fail_self_check(last_heartbeat, current_time, computemgtd_config):
                _self_terminate(computemgtd_config)
            sleep_remaining_loop_time(computemgtd_config.loop_time, current_time)


@retry(wait_fixed=seconds(LOOP_TIME))
def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(module)s:%(funcName)s] %(message)s")
    log.info("Computemgtd Startup")
    try:
        _run_computemgtd()
    except Exception as e:
        log.exception("An unexpected error occurred: %s", e)
        raise


if __name__ == "__main__":
    main()
