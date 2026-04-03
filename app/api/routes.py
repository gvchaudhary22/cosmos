from fastapi import APIRouter
from app.api.endpoints import chat, sessions, tools, admin, actions, feedback, knowledge, costs, health, bridge, brain, graphrag, vectorstore, reportagent, sandbox, training, page_intelligence, hybrid_chat, training_pipeline, tournament, cosmos_settings, learning

router = APIRouter()
router.include_router(chat.router, prefix="/chat", tags=["chat"])
router.include_router(sessions.router, prefix="/sessions", tags=["sessions"])
router.include_router(tools.router, prefix="/tools", tags=["tools"])
router.include_router(admin.router, prefix="/admin", tags=["admin"])
router.include_router(actions.router, prefix="/actions", tags=["actions"])
router.include_router(feedback.router, prefix="/feedback", tags=["feedback"])
router.include_router(knowledge.router, prefix="/knowledge", tags=["knowledge"])
router.include_router(costs.router, prefix="/costs", tags=["costs"])
router.include_router(bridge.router, prefix="/bridge", tags=["bridge"])
router.include_router(brain.router, prefix="/brain", tags=["brain"])
router.include_router(graphrag.router, prefix="/graphrag", tags=["graphrag"])
router.include_router(vectorstore.router, prefix="/vectorstore", tags=["vectorstore"])
router.include_router(reportagent.router, prefix="/reports", tags=["reports"])
router.include_router(sandbox.router, prefix="/sandbox", tags=["sandbox"])
router.include_router(training.router, prefix="/training", tags=["training"])
router.include_router(page_intelligence.router, prefix="/pages", tags=["pages"])
router.include_router(hybrid_chat.router, prefix="/hybrid", tags=["hybrid"])
router.include_router(training_pipeline.router, prefix="/pipeline", tags=["pipeline"])
router.include_router(tournament.router, prefix="/tournament", tags=["tournament"])
router.include_router(cosmos_settings.router, prefix="/settings", tags=["settings"])
router.include_router(learning.router, prefix="/learning", tags=["learning"])

# Health endpoints — exposed at /cosmos/ prefix (not under /api/v1)
health_router = health.router
