"""
Regression tests for issue #21 — soft_required_context for NDR admin APIs.

Verifies that ParamClarificationEngine correctly fires clarification questions
for the 4 NDR APIs that need context:
  - mcapi.v1.admin.ndr.data.get            → asks for date range
  - mcapi.v1.admin.ndr.senddemomessage.post → asks for company_id or AWB
  - mcapi.v1.admin.ndr.get_call_center_recording.by_id.get → asks for recording ID
  - mcapi.v1.admin.ndr.upload_priority.post → asks for CSV file

And that priority_companies.get (no required params) never fires clarification.
"""
import pytest
from app.brain.param_clarifier import (
    ParamClarificationEngine,
    _SoftRequired,
    _APIEntry,
)

NDR_DATA = "mcapi.v1.admin.ndr.data.get"
NDR_RECORDING = "mcapi.v1.admin.ndr.get_call_center_recording.by_id.get"
NDR_DEMO = "mcapi.v1.admin.ndr.senddemomessage.post"
NDR_UPLOAD = "mcapi.v1.admin.ndr.upload_priority.post"
NDR_PRIORITY = "mcapi.v1.admin.ndr.priority_companies.get"


def _engine() -> ParamClarificationEngine:
    engine = ParamClarificationEngine(kb_root="/fake/kb")
    engine._index = {
        NDR_DATA: _APIEntry(
            api_entity_id=NDR_DATA,
            soft_required=[
                _SoftRequired(
                    param="from",
                    alias="",
                    ask_if_missing="What date range should I search for NDR data? Please provide a from and to date.",
                    skip_if_present=["awb"],
                ),
                _SoftRequired(
                    param="to",
                    alias="",
                    ask_if_missing="What end date should I use for the NDR search?",
                    skip_if_present=["awb"],
                ),
            ],
        ),
        NDR_RECORDING: _APIEntry(
            api_entity_id=NDR_RECORDING,
            soft_required=[
                _SoftRequired(
                    param="id",
                    alias="recording_id",
                    ask_if_missing="Which NDR call center recording do you want to retrieve? Please provide the recording ID. Example: 1042",
                    skip_if_present=[],
                ),
            ],
        ),
        NDR_DEMO: _APIEntry(
            api_entity_id=NDR_DEMO,
            soft_required=[
                _SoftRequired(
                    param="company_id",
                    alias="client_id",
                    ask_if_missing="Which company should receive the NDR demo message? Provide company ID. Example: 25149",
                    skip_if_present=["awb"],
                ),
                _SoftRequired(
                    param="awb",
                    alias="awb_code",
                    ask_if_missing="Which AWB number should the NDR demo message be sent for? Provide AWB. Example: SR123456789",
                    skip_if_present=["company_id"],
                ),
            ],
        ),
        NDR_UPLOAD: _APIEntry(
            api_entity_id=NDR_UPLOAD,
            soft_required=[
                _SoftRequired(
                    param="file",
                    alias="csv_file",
                    ask_if_missing="Please upload a CSV file with company NDR priority data. Required columns are company_id, campaign_code, and priority.",
                    skip_if_present=[],
                ),
            ],
        ),
        NDR_PRIORITY: _APIEntry(
            api_entity_id=NDR_PRIORITY,
            soft_required=[],  # no params needed — returns full list
        ),
    }
    return engine


def _chunk(entity_id: str, similarity: float = 0.88) -> dict:
    return {"entity_id": entity_id, "similarity": similarity, "content": "ndr admin"}


# ---------------------------------------------------------------------------
# ndr.data.get — asks for date range when missing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ndr_data_asks_date_range():
    req = await _engine().check(
        knowledge_chunks=[_chunk(NDR_DATA)],
        query="show me NDR data",
        company_id=None,
        session_context={},
    )
    assert req is not None
    assert req.pending_param == "from"
    assert "date" in req.question.lower() or "from" in req.question.lower()


@pytest.mark.asyncio
async def test_ndr_data_no_clarification_when_awb_present():
    """AWB in query → skip_if_present → no date range clarification."""
    req = await _engine().check(
        knowledge_chunks=[_chunk(NDR_DATA)],
        query="NDR status for AWB SR123456789",
        company_id=None,
        session_context={},
    )
    assert req is None


# ---------------------------------------------------------------------------
# ndr.senddemomessage.post — asks for company_id when missing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ndr_demo_asks_company_id():
    req = await _engine().check(
        knowledge_chunks=[_chunk(NDR_DEMO)],
        query="send NDR demo message",
        company_id=None,
        session_context={},
    )
    assert req is not None
    assert req.pending_param == "company_id"
    assert "company" in req.question.lower() or "25149" in req.question


@pytest.mark.asyncio
async def test_ndr_demo_no_clarification_when_company_provided():
    req = await _engine().check(
        knowledge_chunks=[_chunk(NDR_DEMO)],
        query="send NDR demo message",
        company_id="25149",
        session_context={},
    )
    assert req is None


@pytest.mark.asyncio
async def test_ndr_demo_no_clarification_when_awb_in_query():
    """AWB in query skips company_id requirement."""
    req = await _engine().check(
        knowledge_chunks=[_chunk(NDR_DEMO)],
        query="send NDR demo message for AWB SR123456789",
        company_id=None,
        session_context={},
    )
    assert req is None


# ---------------------------------------------------------------------------
# ndr.get_call_center_recording — asks for recording ID
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ndr_recording_asks_for_id():
    req = await _engine().check(
        knowledge_chunks=[_chunk(NDR_RECORDING)],
        query="get NDR call center recording",
        company_id=None,
        session_context={},
    )
    assert req is not None
    assert req.pending_param == "id"
    assert "recording" in req.question.lower() or "id" in req.question.lower()


@pytest.mark.asyncio
async def test_ndr_recording_id_in_session_skips_clarification():
    req = await _engine().check(
        knowledge_chunks=[_chunk(NDR_RECORDING)],
        query="get NDR call center recording",
        company_id=None,
        session_context={"id": "1042"},
    )
    assert req is None


# ---------------------------------------------------------------------------
# ndr.upload_priority.post — asks for CSV file
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ndr_upload_asks_for_file():
    req = await _engine().check(
        knowledge_chunks=[_chunk(NDR_UPLOAD)],
        query="upload NDR priority companies",
        company_id=None,
        session_context={},
    )
    assert req is not None
    assert req.pending_param == "file"
    assert "csv" in req.question.lower() or "upload" in req.question.lower()


# ---------------------------------------------------------------------------
# ndr.priority_companies.get — never fires (empty soft_required)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ndr_priority_companies_no_clarification():
    """priority_companies.get has no required params — never asks anything."""
    req = await _engine().check(
        knowledge_chunks=[_chunk(NDR_PRIORITY)],
        query="show NDR priority companies",
        company_id=None,
        session_context={},
    )
    assert req is None


# ---------------------------------------------------------------------------
# Low similarity — never fires regardless of API
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_low_similarity_no_clarification_ndr():
    req = await _engine().check(
        knowledge_chunks=[_chunk(NDR_DATA, similarity=0.40)],
        query="show NDR data",
        company_id=None,
        session_context={},
    )
    assert req is None
