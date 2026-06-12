from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.deploy import DeployTask
from app.models.log import DeployLog


def publish_log(task_id: str, phase: str, message: str, db: Session | None = None) -> None:
    del db  # 始终使用独立 session，避免与部署流水线共享 session 导致阶段状态无法提交
    if not message or not message.strip():
        return

    log_db = SessionLocal()
    try:
        if log_db.get(DeployTask, task_id) is None:
            return
        entry = DeployLog(task_id=task_id, phase=phase, message=message)
        log_db.add(entry)
        log_db.commit()
    finally:
        log_db.close()
