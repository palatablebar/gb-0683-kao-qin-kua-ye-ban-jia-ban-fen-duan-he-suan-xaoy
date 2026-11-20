"""跨夜召回保障 HTTP 入口：页面与 JSON API。"""
from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request

from app.config import MODULES, SHORT, TITLE
from app.errors import DomainError
from app.modules.m8_recall import service

bp = Blueprint('m8_recall', __name__)


def _json() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise DomainError('PAYLOAD', '请求体必须是 JSON 对象')
    return data


@bp.get('/m8_recall')
def screen():
    service.ensure_rate_calendar()
    service.seed_demo()
    data = service.overview()
    return render_template('m8_recall.html', title=TITLE, short=SHORT,
                           modules=MODULES, current=len(MODULES) + 1, data=data)


@bp.get('/api/m8_recall/events')
def list_events():
    return jsonify(service.overview())


@bp.get('/api/m8_recall/events/<event_id>')
def event_detail(event_id):
    return jsonify(service.event_detail(event_id))


@bp.post('/api/m8_recall/events')
def register():
    return jsonify(service.register_event(_json()))


@bp.post('/api/m8_recall/events/<event_id>/makeup')
def makeup(event_id):
    return jsonify(service.add_makeup_punch(event_id, _json()))


@bp.put('/api/m8_recall/rates')
def update_rates():
    payload = _json()
    calendar = payload.get('rate_calendar')
    affected = payload.get('affected_events')
    if isinstance(affected, str):
        affected = [affected]
    return jsonify(service.update_rate_calendar(calendar, affected))


@bp.post('/api/m8_recall/seed')
def seed():
    event_id = service.seed_demo()
    return jsonify(service.event_detail(event_id))
