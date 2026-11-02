from datetime import UTC, datetime

import pytest
from pydantic import BaseModel

from thermabot.core.types import (
    Action,
    AlertEvent,
    Decision,
    Forecast,
    ForecastPoint,
    HouseState,
    PersonPresence,
    PersonState,
    Plan,
    PlanStep,
    Presence,
    RoomReading,
    Snapshot,
    ThermostatState,
)

T = datetime(2026, 10, 5, 17, 0, tzinfo=UTC)
ENT = "climate.living_room"

ROOM = RoomReading(key="study", temp=70.5, last_reported=T, age_min=3.0, score=100.0)
FC = Forecast(source="ha", fetched_at=T, points=[ForecastPoint(t=T, temp_f=55.0)])
PRES = Presence(
    house=HouseState.OCCUPIED,
    since=T,
    people={"alex": PersonPresence(state=PersonState.HOME, source="life360", age_min=1)},
)
TSTAT = ThermostatState(hvac_mode="heat", current_temp=69.0, heat_sp=68.0, last_reported=T)
STEP = PlanStep(t=T, hvac_mode="heat", heat_sp=68, pred_rooms={"study": 69.0}, reason="band")
ACT = Action(service="set_temperature", entity_id=ENT, data={"temperature": 68.0})

MODELS: list[BaseModel] = [
    ROOM,
    FC,
    PRES,
    TSTAT,
    STEP,
    Snapshot(t=T, thermostat=TSTAT, rooms={"study": ROOM}, presence=PRES, forecast=FC),
    Plan(kind="optimised", created_at=T, steps=[STEP]),
    ACT,
    Decision(t=T, action=ACT, allowed=False, reason="shadow", would_write=True),
    AlertEvent(key="room_stale:bedroom_1", severity="warn", text="check sensor battery", t=T),
]


@pytest.mark.brd("D§11.2")
@pytest.mark.parametrize("m", MODELS, ids=lambda m: type(m).__name__)
def test_json_round_trip(m: BaseModel) -> None:
    again = type(m).model_validate_json(m.model_dump_json())
    assert again == m
