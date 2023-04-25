from clearml import Task
from clearml.automation import (
    DiscreteParameterRange, HyperParameterOptimizer, RandomSearch, GridSearch,
    UniformParameterRange,UniformIntegerParameterRange)

import argparse
from clearml.automation.optuna import OptimizerOptuna
from clearml.automation.hpbandster import OptimizerBOHB
import json
from time import sleep
from usage_reporter import UsageReporter
from clearml.backend_api.session import Session

reporting_units = 1

def num(s):
    try:
        return int(s)
    except ValueError:
        try:
            return float(s)
        except:
            return s


def job_complete_callback(
    job_id,                 # type: str
    objective_value,        # type: float
    objective_iteration,    # type: int
    job_parameters,         # type: dict
    top_performance_job_id  # type: str
):
    print('Job completed!', job_id, objective_value, objective_iteration, job_parameters)
    if job_id == top_performance_job_id:
        print('WOOT WOOT we broke the record! Objective reached {}'.format(objective_value))


def main():
    # Connecting ClearML with the current process,
    # from here on everything is logged automatically
    task = Task.init(project_name='HPO APP',
                     task_name='Optimization',
                     task_type=Task.TaskTypes.optimizer,
                     reuse_last_task_id=False)
    session: Session = task.session

    reporter = UsageReporter(session=session, app_id="project-dashboard", report_interval_sec=60, units=max(1, reporting_units))

    parser = argparse.ArgumentParser(description='ClearML HPO')
    parser.add_argument('--base_task_id', type=str,
                        help='Base Task ID to Optimize')
    parser.add_argument('--strategy', type=str,
                        help='Search Strategy Type (Optuna, BOHB, Grid, Random)')
    parser.add_argument('--max_concurrent', type=int, default=1,
                        help='Maximum experiments running in parallel')
    parser.add_argument('--metric_title', type=str, default='',
                        help='Metric to optimize')
    parser.add_argument('--metric_series', type=str, default='',
                        help='Metric to optimize')
    parser.add_argument('--metric_sign', type=str, default='max',
                        help='Metric objective\'s sign (max / min)')
    parser.add_argument('--queue', type=str, default='default',
                        help='Queue to enqueue Tasks to')
    parser.add_argument('--save_top_k_exp', type=int, default=None,
                        help='Top K experiments to save, the rest will automatically be archived')
    parser.add_argument('--time_per_job', type=int, default=6000,
                        help='Time limit (in min) per experiment')
    parser.add_argument('--min_iter_per_job', type=int, default=1,
                        help='Minimum iterations per Experiment before early stopping')
    parser.add_argument('--max_iter_per_job', type=int, default=10000,
                        help='Maximum iterations per Experiment')
    parser.add_argument('--max_jobs', type=int, default=30,
                        help='Maximum amount of Experiments per HPO')
    parser.add_argument('--tot_time', type=int, default=1000000,
                        help='Total Optimization Process Time')
    parser.add_argument('--project', type=str, default=None,
                        help='All Tasks created by the HPO process will be created under this project')
    args = parser.parse_args()

    task.set_parameter(name='Args/combined_name', value=args.metric_title + '/' + args.metric_series)

    # Set search strategy
    strategy = args.strategy
    if strategy == 'Bandit-Based Bayesian Optimization w/ TPE (With Optuna)':
        search_strat = OptimizerOptuna
    elif strategy == 'HyperBand Bayesian Optimization':
        search_strat = OptimizerBOHB
    elif strategy == 'Random':
        search_strat = RandomSearch
    elif strategy == 'Grid':
        search_strat = GridSearch
    else:
        print('Invalid strategy')
        exit(1)

    # Getting Parameter list
    params_config = task.connect_configuration('params_list.json', name='params_list')
    params_list = json.load(open(params_config, 'rt'))

    hp_list = []

    for param in params_list:
        if param['param_type'] == "Discrete":
            val_list = []
            values = param['discrete_values'].split(',')
            for val in values:
                val_list.append(num(val))
            hp_list.append(DiscreteParameterRange(param['param_name'], values=val_list))
        elif param['param_type'] == "Uniform":
            # float parameter
            if '.' in param['min_val'] or '.' in param['max_val'] or '.' in param['step_size']:
                hp_list.append(UniformParameterRange(param['param_name'],
                                                     min_value=float(param['min_val']),
                                                     max_value=float(param['max_val']),
                                                     step_size=float(param['step_size'])))
            # integer parameter
            else:
                hp_list.append(UniformIntegerParameterRange(param['param_name'],
                                                            min_value=int(param['min_val']),
                                                            max_value=int(param['max_val']),
                                                            step_size=int(param['step_size'])))


    sleep(5)
    print('number of params: ' + str(len(hp_list)))


    an_optimizer = HyperParameterOptimizer(
        base_task_id=args.base_task_id,
        hyper_parameters=hp_list,
        # this is the objective metric we want to maximize/minimize
        objective_metric_title=args.metric_title,
        objective_metric_series=args.metric_series,
        objective_metric_sign=args.metric_sign,
        max_number_of_concurrent_tasks=args.max_concurrent,
        optimizer_class=search_strat,
        execution_queue=args.queue,
        # If specified all Tasks created by the HPO process will be created under the `spawned_project` project
        spawn_project=args.project or None,  # 'HPO spawn project',
        # If specified only the top K performing Tasks will be kept, the others will be automatically archived
        save_top_k_tasks_only=args.save_top_k_exp,  # 5,
        time_limit_per_job=args.time_per_job,
        pool_period_min=0.2,
        total_max_jobs=args.max_jobs,
        min_iteration_per_job=args.min_iter_per_job,
        max_iteration_per_job=args.max_iter_per_job,
        optimization_time_limit=args.tot_time
    )

    # report every 2 minutes
    an_optimizer.set_report_period(2)
    an_optimizer.start(job_complete_callback=job_complete_callback)
    # set the time limit for the optimization process (2 hours)
    #an_optimizer.set_time_limit(in_minutes=120.0)
    # wait until process is done (notice we are controlling the optimization process in the background)
    an_optimizer.wait()
    # optimization is completed, print the top performing experiments id
    #top_exp = an_optimizer.get_top_experiments(top_k=3)
    #print([t.id for t in top_exp])
    an_optimizer.stop()


if __name__ == "__main__":
    main()

