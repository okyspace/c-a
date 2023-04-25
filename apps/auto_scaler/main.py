import json
from argparse import ArgumentParser
from os import environ

from clearml import Task
from clearml.automation.auto_scaler import ScalerConfig
from clearml.backend_api.session import Session
from clearml.backend_config.converters import safe_text_to_bool
from clearml.debugging import get_logger

from auto_scaler import AutoScaler
from aws_driver import AWSDriver
from gcp_driver import GCPDriver
from usage_reporter import UsageReporter

providers = {
    'AWS': AWSDriver,
    'GCP': GCPDriver,
}

reporting_units = 1


def create_config(configurations, hyper_params):
    config_dict = {'extra_vm_bash_script': "", 'extra_clearml_conf': ""}
    for section, conf in configurations.items():
        if section == "resource_configurations":
            config_dict[section] = {resource_dict["resource_name"]: resource_dict for resource_dict in json.loads(conf)}
            resources = config_dict[section].keys()
            for resource in resources:
                if config_dict[section][resource].get("security_group_ids"):
                    config_dict[section][resource]["security_group_ids"] = \
                        config_dict[section][resource].get("security_group_ids").split(",")
        else:
            config_dict[section] = conf
    config = {
        'configurations': config_dict,
        'hyper_params': hyper_params,
    }

    # Add queues to config
    config['configurations']['queues'] = {
        resource_conf["queue_name"]: [(resource_name, resource_conf["num_instances"])]
        for resource_name, resource_conf in config['configurations']['resource_configurations'].items()
    }
    return config


def main():
    # Support --help
    parser = ArgumentParser(description='Run auto scaler')
    parser.parse_args()

    task_name = 'Auto-Scaler'
    task = Task.init(project_name='DevOps', task_name=task_name, task_type=Task.TaskTypes.service)
    session: Session = task.session

    reporter = UsageReporter(
        session=session, app_id="project-dashboard", report_interval_sec=60, units=max(1, reporting_units)
    )
    hyper_params = {
        param_name.partition("/")[2]: int(param_val) if param_val.isdigit() else
        param_val if param_val not in ("True", "False") else
        safe_text_to_bool(param_val)
        for param_name, param_val in task.get_parameters().items()
    }
    hyper_params["workers_prefix"] = "{}_{}".format(Task.current_task().task_id[:4], hyper_params["workers_prefix"])
    provider = hyper_params.get('cloud_provider')
    task.add_tags(provider)
    Driver = providers.get(provider)
    if Driver is None:
        raise SystemError('error: unknown driver - {provider!r}')

    # task.connect(hyper_params)  # Show in Task UI
    configurations = task.get_configuration_objects()

    config = create_config(configurations, hyper_params)

    driver = Driver.from_config(config)
    scaler_config = ScalerConfig.from_config(config)
    scaler = AutoScaler(
        scaler_config,
        driver,
        logger=get_logger(task_name, level=environ.get('LOG_LEVEL', 'INFO'))
    )
    scaler.start()


if __name__ == '__main__':
    main()
