"""External decision boundary. The engine remains the only assignment writer."""
from __future__ import annotations
import copy
import hmac
import math
import os
import time
from typing import Literal
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .schema import DecisionCandidate
from .sim.engine import SIM, TH


class Assignment(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    decision_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    observed_tick: int = Field(ge=0, strict=True)
    task_id: str = Field(min_length=1, max_length=128)
    robot_id: str = Field(min_length=1, max_length=128)
    goal_id: Literal['G_WT_TRANSPORT']
    model_task_id: Literal['T_ORDER_ASSIGN']
    planned_lift_id: str | None = Field(default=None, max_length=128)
    candidates: list[DecisionCandidate] = Field(min_length=1, max_length=5)
    weights: dict[str, float]

    @model_validator(mode='after')
    def finite_audit_values(self):
        for candidate in self.candidates:
            if not all(math.isfinite(getattr(candidate, key)) for key in ('score', 'distance_m', 'battery', 'congestion', 'health')):
                raise ValueError('Candidate values must be finite')
        if not all(math.isfinite(value) for value in self.weights.values()):
            raise ValueError('Weights must be finite')
        return self


def authorize(key):
    expected = os.getenv('FLEET_API_KEY', '')
    if not expected or not hmac.compare_digest(key or '', expected):
        raise HTTPException(401, 'Invalid fleet service key')


def dispatch_snapshot(server):
    state = server.engine.state
    return copy.deepcopy({
        'run_id': server.run_id, 'tick': state['sim']['tick'], 'published_at': time.time(),
        'mode': server.assignment_mode, 'paused': server.paused or server.speed == 0,
        'layout_id': state['layout_id'], 'namespace': server.mqtt.namespace,
        'constants': SIM, 'robot_order': list(state['robots']),
        'state': {'tasks': {k: t for k, t in state['tasks'].items() if t['status'] == 'WAITING'},
                  'robots': state['robots'], 'lifts': state['lifts'], 'zones': state['zones']},
    })


def apply_assignment(server, body):
    """Synchronous validation + commit: no await between check and mutation."""
    data = body.model_dump()
    def conflict(message): raise HTTPException(409, message)
    if server.assignment_mode != 'external': conflict('External allocation disabled')
    if body.run_id != server.run_id: conflict('Simulation run changed')
    previous = server.assignment_results.get(body.decision_id)
    if previous:
        if previous[0] != data: conflict('Decision ID reused with different content')
        return copy.deepcopy(previous[1])
    if server.paused or server.speed == 0: conflict('Simulation paused')
    published = server.dispatch_ticks.get(body.observed_tick)
    if published is None or not 0 <= time.time() - published <= 5:
        conflict('Dispatch snapshot expired or unknown')
    engine = server.engine
    task = engine.state['tasks'].get(body.task_id)
    robot = engine.state['robots'].get(body.robot_id)
    if not task or task['status'] != 'WAITING' or task['assigned_robot'] is not None:
        conflict('Task no longer waiting')
    if (not robot or robot['fsm'] != 'IDLE' or robot['status'] in ('OFFLINE', 'ERROR')
        or robot['current_task_id'] is not None or robot['battery'] <= TH['BATTERY_WARNING']
        or robot['load']['current'] != 0 or not 0 < task['load_units'] <= robot['load']['capacity']):
        conflict('Robot no longer eligible')
    if task['source'] not in engine.loc or task['destination'] not in engine.loc:
        conflict('Invalid task locations')
    if body.planned_lift_id is not None:
        lift = engine.state['lifts'].get(body.planned_lift_id)
        if not lift or lift['fault']: conflict('Planned lift unavailable')
    if body.candidates[0].robot_id != body.robot_id:
        conflict('Selected robot differs from first candidate')
    engine.commit_assignment(task, robot, body.planned_lift_id,
                             [c.model_dump() for c in body.candidates], body.weights,
                             narrative=f'External decision {body.decision_id}; goal {body.goal_id}; contract {body.model_task_id}')
    result = {'accepted': True, 'decision_id': body.decision_id, 'run_id': server.run_id,
              'assigned_tick': task['assigned_tick'], 'context': {'Task': copy.deepcopy(task), 'Robot': copy.deepcopy(robot)}}
    server.assignment_results[body.decision_id] = (data, result)
    return copy.deepcopy(result)
