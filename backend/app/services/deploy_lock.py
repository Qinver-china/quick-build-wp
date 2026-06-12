from app.core.database import SessionLocal
from app.core.redis_client import get_redis
from app.models.deploy import DeployStatus, DeployTask

LOCK_PREFIX = "deploy:lock:"
HOST_LOCK_PREFIX = "deploy:host_lock:"
LOCK_TTL_SECONDS = 7200

_ACTIVE_STATUSES = (DeployStatus.PENDING, DeployStatus.RUNNING)


def _host_lock_key(host: str, port: int) -> str:
    return f"{HOST_LOCK_PREFIX}{host}:{port}"


def is_deploy_task_active(task_id: str) -> bool:
    """任务是否仍在排队或执行中。"""
    db = SessionLocal()
    try:
        task = db.get(DeployTask, task_id)
        if not task:
            return False
        return task.status in _ACTIVE_STATUSES
    finally:
        db.close()


def release_stale_host_lock(host: str, port: int) -> bool:
    """清除指向已结束/已删除任务的陈旧主机锁。"""
    key = _host_lock_key(host, port)
    redis_client = get_redis()
    current = redis_client.get(key)
    if not current or is_deploy_task_active(current):
        return False
    redis_client.delete(key)
    return True


def acquire_host_lock(host: str, port: int, task_id: str, *, force: bool = False) -> bool:
    """同一目标服务器同时只允许一个部署任务执行。"""
    key = _host_lock_key(host, port)
    redis_client = get_redis()
    if force:
        redis_client.set(key, task_id, ex=LOCK_TTL_SECONDS)
        return True
    current = redis_client.get(key)
    if current == task_id:
        redis_client.expire(key, LOCK_TTL_SECONDS)
        return True
    if current and not is_deploy_task_active(current):
        redis_client.delete(key)
        current = None
    if current:
        return False
    return bool(redis_client.set(key, task_id, nx=True, ex=LOCK_TTL_SECONDS))


def release_host_lock(host: str, port: int, task_id: str) -> None:
    key = _host_lock_key(host, port)
    redis_client = get_redis()
    if redis_client.get(key) == task_id:
        redis_client.delete(key)


def clear_host_lock(host: str, port: int) -> None:
    get_redis().delete(_host_lock_key(host, port))


def acquire_deploy_lock(task_id: str, owner: str, *, force: bool = False) -> bool:
    key = f"{LOCK_PREFIX}{task_id}"
    redis_client = get_redis()
    if force:
        redis_client.set(key, owner, ex=LOCK_TTL_SECONDS)
        return True
    current = redis_client.get(key)
    if current == owner:
        redis_client.expire(key, LOCK_TTL_SECONDS)
        return True
    if current and not is_deploy_task_active(task_id):
        redis_client.delete(key)
        current = None
    if current:
        return False
    return bool(redis_client.set(key, owner, nx=True, ex=LOCK_TTL_SECONDS))


def clear_deploy_lock(task_id: str) -> None:
    get_redis().delete(f"{LOCK_PREFIX}{task_id}")


def release_deploy_lock(task_id: str, owner: str) -> None:
    key = f"{LOCK_PREFIX}{task_id}"
    redis_client = get_redis()
    if redis_client.get(key) == owner:
        redis_client.delete(key)


def refresh_deploy_lock(task_id: str, owner: str) -> None:
    key = f"{LOCK_PREFIX}{task_id}"
    redis_client = get_redis()
    if redis_client.get(key) == owner:
        redis_client.expire(key, LOCK_TTL_SECONDS)
