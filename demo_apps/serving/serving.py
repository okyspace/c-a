import json

from clearml import Task

import plotly.graph_objects as go
import plotly.express as px
import argparse
from time import sleep

DEBUG = False


parser = argparse.ArgumentParser(description='ClearML HPO')
parser.add_argument('--model_id', type=str,
                    help='Model ID')
parser.add_argument('--endpoint', type=str, default='endpoint',
                    help='Model Endpoint')
parser.add_argument('--queue', type=str, default="default",
                    help='Execution Queue')

args = parser.parse_args()

if not DEBUG:
    task = Task.init(project_name='serving',task_name='mock serving')
    logger = task.get_logger()

## Latency
TEXT = "Latency (ms)"
VAL = 789
fig = go.Figure()
fig.add_trace(go.Indicator(
    domain={'row': 1, 'column': 0},
    #title={'text': TEXT},
    value=VAL,
    mode="gauge+number",
    gauge={'bar': {'color': "#787878"},
           'bordercolor': '#39405F',
           'steps': [
               {'range': [0, 1000], 'color': "lightgray"},
           ],
           'axis': {'range': [None, 1000],
                    'tickcolor': '#39405E', 'dtick': 50},
           'bgcolor': 'rgba(0,0,0,0)'}
))

if DEBUG:
    fig.show()
else:
    logger.report_plotly(title=TEXT, series='', figure=fig)
#task.get_logger().report_plotly(title='jobs', series='running', figure=fig)
## Predictions \ Minute
TEXT = "Perdictions Per Minute"
VAL = 251
fig = go.Figure()
fig.add_trace(go.Indicator(
    domain={'row': 1, 'column': 0},
    title={'text': TEXT},
    value=VAL,
    mode="gauge+number",
    gauge={'bar': {'color': "#787878"},
           'bordercolor': '#39405F',
           'steps': [
               {'range': [0, 1000], 'color': "lightgray"},
           ],
           'axis': {'range': [None, 1000],
                    'tickcolor': '#39405E', 'dtick': 50},
           'bgcolor': 'rgba(0,0,0,0)'}
))

if DEBUG:
    fig.show()
else:
    logger.report_plotly(title=TEXT, series='', figure=fig)


TEXT = "Distribution"
data_canada = px.data.gapminder().query("country == 'Canada'")
fig = px.bar(data_canada, x='year', y='pop')
if DEBUG:
    fig.show()
else:
    logger.report_plotly(title=TEXT, series='', figure=fig)

if not DEBUG:
    import random
    for i, val in enumerate(range(1,random.randrange(1,1000))):
        logger.report_scalar(iteration=i,value=random.randrange(3500,5000),title="Scalar",series="1")


while True:
    sleep(60)
    print('hi')
