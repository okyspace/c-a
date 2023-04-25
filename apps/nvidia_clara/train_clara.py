import argparse
import logging
import os
import sys

import plotly.graph_objects as go

from clearml import Dataset, Task
from medl.apps.train import main as main_train_mmar
from pathlib2 import Path
from usage_reporter import UsageReporter
from clearml.backend_api.session import Session

reporting_units = 1

def parse_known_args_only(self, args=None, namespace=None):
    return self.parse_known_args(args=None, namespace=None)[0]


argparse.ArgumentParser.parse_args = parse_known_args_only


def get_plotly_for_models(task, models):
    names = []
    paths = []
    for model in models:
        names.append(model.name)
        paths.append(model.url)
    fig = go.Figure(data=[go.Table(header=dict(values=['Model Name', 'Model Path']),
                                   cells=dict(values=[names, paths]))
                          ])
    task.get_logger().report_plotly(
        title="Models Table",
        series="Clara Training Models",
        figure=fig
    )


def set_env_vars():
    os.environ["PYTHONPATH"] = "{}:/opt/nvidia:".format(os.environ["PYTHONPATH"])
    os.environ["MMAR_ROOT"] = os.getcwd()


def upload_models(task, models_dir):
    logger = task.get_logger()
    models = []
    logger.report_text("Getting models from {} dir: {}".format(models_dir, os.listdir(models_dir)))
    for f in Path(models_dir).rglob('*.pt'):
        logger.report_text("Uploading {} as artifact".format(f.name))
        task.upload_artifact(f.name, artifact_object=f, wait_on_upload=True)
        # noinspection PyBroadException
        try:
            models.append(task.artifacts[f.name])
        except Exception as ex:
            logger.report_text(" {}".format(ex), level=logging.WARNING)
    get_plotly_for_models(task, models)


def main():
    task = Task.init(project_name="Nvidia Clara V4 examples with ClearML",
                     task_name="Training with Clara V4",
                     reuse_last_task_id=False)
    task.set_base_docker(docker_cmd="nvcr.io/nvidia/clara-train-sdk:v4.0", docker_arguments="--ipc=host")
    session: Session = task.session

    reporter = UsageReporter(session=session, app_id="project-dashboard", report_interval_sec=60, units=max(1, reporting_units))
    parser = argparse.ArgumentParser()
    parser.add_argument("--mmar", "-m", type=str, help="MMAR_ROOT folder")
    parser.add_argument("--train_config", "-c", type=str, help="train config file")
    parser.add_argument("--arch", "-a", type=str, help="Clara arch")
    parser.add_argument("--env", "-e", type=str, help="environment file")
    parser.add_argument("--log_config", "-l", type=str, help="log config file")
    parser.add_argument("--write_train_stats", action="store_true")
    parser.add_argument("--set", metavar="KEY=VALUE", nargs="*")
    parser.add_argument("--images_dir", type=str,
                        help="Name of the images folder, will be store as a folder in DATA_ROOT."
                             "Should be the same to the artifact name in the dataset task")
    parser.add_argument("--labels_dir", type=str,
                        help="Name of the labels folder, will be store as a folder in DATA_ROOT."
                             "Should be the same to the artifact name in the dataset task")
    parser.add_argument("--dataset_task", type=str,
                        help="The dataset task id, if not provided, a task named `Example data` will be chosen")

    set_env_vars()
    args = parser.parse_args()
    mmar = args.mmar or os.environ["MMAR_ROOT"]
    arch = args.arch
    env = args.env
    log_config = args.log_config
    kv = args.set
    images_dir = args.images_dir or ""
    labels_dir = args.labels_dir or ""
    dataset_task = args.dataset_task

    if dataset_task:
        dataset_task = Dataset.get(dataset_id=dataset_task)
    else:
        dataset_task = Dataset.get(dataset_project="Nvidia Clara examples with ClearML",
                                   dataset_name="Example data")
    updated_kv = []
    models_dir = None
    local_data = dataset_task.get_local_copy()
    for elem in kv:
        if elem.startswith("DATASET_JSON"):
            dataset_name = elem.rpartition("/")[2]
            updated_kv.append("DATASET_JSON={}".format(os.path.join(local_data, dataset_name)))
        elif elem.startswith("MMAR_CKPT_DIR"):
            models_dir = elem.partition("=")[2]
            updated_kv.append(elem)
        else:
            updated_kv.append(elem)

    train_config = "config/trn_Unet.json" if arch == "UNet" else "config/trn_SegResNet.json"
    train_conf = task.connect_configuration(train_config, name="train", description="train config file")
    if env:
        env_conf = task.connect_configuration(env, name="env", description="environment file")

        with open(env_conf, "r") as env_file:
            import json
            env_dict = json.load(env_file)
            data_root = env_dict.get("DATA_ROOT", "/")
            # noinspection PyBroadException
            try:
                os.makedirs(os.path.join(mmar, data_root))
            except Exception:
                pass
            dataset_json = env_dict.get("DATASET_JSON", "/")
            try:
                dataset_json_file = task.connect_configuration(os.path.join(mmar, dataset_json),
                                                               name="dataset_json",
                                                               description="dataset file")
                # noinspection PyBroadException
                try:
                    os.makedirs(dataset_json.rpartition("/")[0])
                except Exception:
                    pass
                os.system("cp -R {} {}".format(dataset_json_file, os.path.join(mmar, dataset_json)))
            except Exception as ex:
                print("Can not connect dataset config file {},\n{}".format(dataset_json, ex))
        for artifact in os.listdir(local_data):
            os.system("cp -R {} {}".format(os.path.join(local_data, artifact), str(os.path.join(mmar, data_root))))
            if (artifact == images_dir and images_dir) or (artifact == labels_dir and labels_dir):
                os.system("mv {} {}".format(os.path.join(local_data, artifact),
                                            os.path.join(mmar, data_root, artifact)))
    else:
        env_conf = env

    log_conf = task.connect_configuration(log_config, name="log config",
                                          description="log config file") if log_config else log_config
    # noinspection PyBroadException
    try:
        models_dir = os.path.join(mmar, models_dir or train_config.rpartition("/")[0])
        os.makedirs(models_dir)
    except Exception:
        models_dir = mmar

    # noinspection PyBroadException
    try:
        os.makedirs(os.path.join(mmar, train_config.rpartition("/")[0]))
    except Exception:
        pass
    os.system("cp -R {} {}".format(train_conf, os.path.join(mmar, train_config)))
    sys.argv.extend(['--train_config', os.path.join(mmar, train_config)])

    # noinspection PyBroadException
    try:
        os.makedirs(os.path.join(mmar, env.rpartition("/")[0]))
    except Exception:
        pass
    os.system("cp -R {} {}".format(env_conf, os.path.join(mmar, env)))
    # noinspection PyBroadException
    try:
        os.makedirs(os.path.join(mmar, log_config.rpartition("/")[0]))
    except Exception:
        pass
    os.system("cp -R {} {}".format(log_conf, os.path.join(mmar, log_config)))
    main_train_mmar()
    upload_models(task, models_dir)


if __name__ == "__main__":
    main()
