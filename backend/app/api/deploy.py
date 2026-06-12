from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.crypto import encrypt
from app.core.database import get_db
from app.models.deploy import DeployPhase, DeployStatus, DeployTask
from app.schemas.deploy import (
    DeployCancelResponse,
    DeployCreateRequest,
    DeployCreateResponse,
    DeployRetryResponse,
    DeployStatusResponse,
    PreflightRequest,
    PreflightResponse,
    SSHTestRequest,
    SSHTestResponse,
)
from app.services.deploy_cancel import clear_deploy_cancelled, purge_deploy_task
from app.services.deploy_lock import clear_deploy_lock, release_host_lock, release_stale_host_lock
from app.services.log_publisher import publish_log
from app.services.preflight import run_preflight_with_timeout
from app.tasks.deploy_pipeline import run_deploy_pipeline

router = APIRouter(prefix="/api/deploy", tags=["deploy"])

PHASE_LABELS = {
    DeployPhase.STEP1_BAOTA: "安装宝塔",
    DeployPhase.STEP2_NGINX: "安装 Nginx",
    DeployPhase.STEP3_PHP: "安装 PHP",
    DeployPhase.STEP2_PHP: "安装 PHP",
    DeployPhase.STEP3_MYSQL: "安装 MySQL",
    DeployPhase.STEP4_REDIS: "安装 Redis",
    DeployPhase.STEP5_PHP_EXT: "安装 PHP 组件与扩展",
    DeployPhase.STEP6_OPTIMIZE: "参数调优",
    DeployPhase.STEP7_SITE: "创建网站并安装 WordPress",
    DeployPhase.STEP8_SSL: "申请 SSL 证书",
    DeployPhase.DONE: "完成",
    DeployPhase.STEP2_LNMP: "安装 PHP",
    DeployPhase.STEP2_OPTIMIZE: "参数调优",
    DeployPhase.STEP2_REDIS: "安装 Redis",
    DeployPhase.STEP3_SITE: "创建网站并安装 WordPress",
    DeployPhase.STEP4_WORDPRESS: "创建网站并安装 WordPress",
    DeployPhase.STEP5_VERIFY: "创建网站并安装 WordPress",
}


def _to_preflight_response(result) -> PreflightResponse:
    return PreflightResponse(**result.to_dict())


@router.post("/preflight", response_model=PreflightResponse)
def preflight_check(req: PreflightRequest):
    domains = req.site_domains
    if not domains and req.site_domain:
        domains = [req.site_domain.strip().lower()]
    result = run_preflight_with_timeout(
        host=req.ssh_host.strip(),
        port=req.ssh_port,
        username=req.ssh_user,
        password=req.ssh_password,
        server_os=req.server_os,
        site_domain=domains[0] if domains else None,
        site_domains=domains,
        php_version=req.php_version,
    )
    return _to_preflight_response(result)


@router.post("", response_model=DeployCreateResponse)
def create_deploy(req: DeployCreateRequest, request: Request, db: Session = Depends(get_db)):
    client_ip = request.client.host if request.client else None

    normalized_sites = req.normalized_sites()
    all_domains = req.all_domains()
    preflight = run_preflight_with_timeout(
        host=req.ssh_host.strip(),
        port=req.ssh_port,
        username=req.ssh_user,
        password=req.ssh_password,
        server_os=req.server_os,
        site_domain=all_domains[0] if all_domains else None,
        site_domains=all_domains,
        php_version=req.php_version,
    )
    if not preflight.ssh_ok:
        raise HTTPException(status_code=400, detail=preflight.message or "SSH 连接失败")
    if preflight.blocked or preflight.domain_conflict:
        raise HTTPException(status_code=400, detail=preflight.message)
    if not preflight.ok:
        raise HTTPException(status_code=400, detail=preflight.message)
    if preflight.requires_confirmation and not req.confirm_non_fresh:
        raise HTTPException(
            status_code=400,
            detail="服务器不是全新环境，请在前端确认风险提示后再继续安装",
        )

    active = (
        db.query(DeployTask)
        .filter(
            DeployTask.ssh_host == req.ssh_host.strip(),
            DeployTask.ssh_port == req.ssh_port,
            DeployTask.status.in_([DeployStatus.PENDING, DeployStatus.RUNNING]),
        )
        .first()
    )
    if active:
        raise HTTPException(
            status_code=409,
            detail="该服务器已有进行中的部署任务，请等待完成或终止后再试",
        )

    release_stale_host_lock(req.ssh_host.strip(), req.ssh_port)

    primary = normalized_sites[0]
    effective_php = preflight.php_version_effective or req.php_version
    php_requested = preflight.php_version_requested if preflight.php_version_fallback else None
    task = DeployTask(
        ssh_host=req.ssh_host.strip(),
        ssh_port=req.ssh_port,
        ssh_user=req.ssh_user,
        server_os=req.server_os,
        confirm_non_fresh=req.confirm_non_fresh,
        ssh_password_enc=encrypt(req.ssh_password),
        bt_user=req.resolved_bt_user(),
        bt_password=req.resolved_bt_password(),
        bt_port=req.bt_port,
        bt_safe_path=req.resolved_bt_safe_path(),
        nginx_version=req.nginx_version,
        php_version=effective_php,
        php_version_requested=php_requested,
        mysql_version=req.mysql_version,
        site_name=primary["site_name"],
        site_domain=primary["primary_domain"],
        site_input=primary["primary_domain"],
        wp_admin_user=primary["wp_admin_user"],
        wp_admin_password=primary["wp_admin_password"],
        wp_password_auto_generated=primary.get("wp_password_auto_generated", False),
        wp_admin_email=primary["wp_admin_email"],
        wp_locale=primary.get("wp_locale", req.wp_locale),
        db_prefix=primary.get("db_prefix"),
        db_name=primary.get("db_name"),
        db_user=primary.get("db_user"),
        db_password=primary.get("db_password"),
        sites_config=normalized_sites,
        client_ip=client_ip,
        status=DeployStatus.PENDING,
        current_phase=DeployPhase.STEP1_BAOTA,
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    run_deploy_pipeline.apply_async(args=[task.id], task_id=task.id)

    return DeployCreateResponse(
        token=task.token,
        progress_url=f"/deploy/{task.token}",
    )


@router.post("/{token}/cancel", response_model=DeployCancelResponse)
def cancel_deploy(token: str, db: Session = Depends(get_db)):
    task = db.query(DeployTask).filter(DeployTask.token == token).first()
    if not task:
        return DeployCancelResponse(ok=True, message="任务不存在或已删除")

    purge_deploy_task(db, task)
    return DeployCancelResponse(ok=True, message="任务已终止并删除")


@router.post("/{token}/retry", response_model=DeployRetryResponse)
def retry_deploy(token: str, db: Session = Depends(get_db)):
    task = db.query(DeployTask).filter(DeployTask.token == token).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    if task.status != DeployStatus.FAILED:
        raise HTTPException(status_code=400, detail="仅失败状态的任务可以重试")

    if not task.ssh_password_enc:
        raise HTTPException(
            status_code=400,
            detail="SSH 密码已不可用，无法重试。请终止任务后重新创建部署。",
        )

    phase_label = PHASE_LABELS.get(task.current_phase, task.current_phase.value)
    clear_deploy_cancelled(task.id)
    clear_deploy_lock(task.id)
    release_host_lock(task.ssh_host, task.ssh_port, task.id)

    task.status = DeployStatus.PENDING
    task.error_message = None
    task.result = None
    db.commit()

    publish_log(
        task.id,
        "system",
        f"用户发起重试，将从「{phase_label}」继续执行（已完成步骤将自动跳过）",
        db,
    )

    run_deploy_pipeline.apply_async(args=[task.id], kwargs={"recovery": True}, task_id=task.id)

    return DeployRetryResponse(
        ok=True,
        message=f"已从「{phase_label}」重新执行",
        current_phase=task.current_phase.value,
        user_step_label=phase_label,
    )


@router.get("/{token}", response_model=DeployStatusResponse)
def get_deploy_status(token: str, db: Session = Depends(get_db)):
    task = db.query(DeployTask).filter(DeployTask.token == token).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    expired = False
    if task.expires_at:
        exp = task.expires_at.replace(tzinfo=None) if task.expires_at.tzinfo else task.expires_at
        expired = exp < datetime.utcnow()

    return DeployStatusResponse(
        token=task.token,
        status=task.status.value,
        current_phase=task.current_phase.value,
        user_step=task.user_step,
        user_step_label=PHASE_LABELS.get(task.current_phase, ""),
        error_message=task.error_message,
        result=task.result,
        created_at=task.created_at,
        updated_at=task.updated_at,
        expired=expired,
    )


@router.post("/test-ssh", response_model=SSHTestResponse)
def test_ssh(req: SSHTestRequest):
    result = run_preflight_with_timeout(
        host=req.ssh_host.strip(),
        port=req.ssh_port,
        username=req.ssh_user,
        password=req.ssh_password,
        server_os=req.server_os,
    )
    return SSHTestResponse(
        ok=result.ok and result.ssh_ok,
        message=result.message,
        os_info=result.uname or result.os_pretty,
        preflight=_to_preflight_response(result),
    )
