import json

from clearml import Task
from clearml.automation import TaskScheduler

def cast(num):
    try:
        return int(num)
    except:
        return None


task = Task.init(project_name='DevOps',task_name='Task Scheduler')

schedules_file = task.connect_configuration('schedules.json',name='Schedules')


if not schedules_file:
    exit(1)

schedules = json.load(open(schedules_file, 'rt'))



schedulder = TaskScheduler()

for item in schedules:
    weekdays = None
    if item['weekdays']:
        weekdays = [cast(t) for t in item['weekdays'].split(',')]
    if item['recurring'] == 'Yes':
        recurring = True
    else:
        recurring = False

    schedulder.add_task(schedule_task_id=item['task_id'],
                        #schedule_function='bla',
                        queue=item['queue'],
                        name=item['task_name'],
                        target_project=item['target_project'],
                        minute=cast(item['minute']),
                        hour=cast(item['hour']),
                        day=cast(item['day']),
                        weekdays=weekdays,
                        month=cast(item['month']),
                        year=cast(item['year']),
                        recurring=recurring,
                        #task_parameters=item['task_parameters']
                        )

schedulder.start()