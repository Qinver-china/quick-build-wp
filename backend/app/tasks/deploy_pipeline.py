from datetime import datetime

from app.core.crypto import decrypt
from app.core.database import SessionLocal
from app.models.deploy import DeployPhase, DeployStatus, DeployTask
from app.services import baota_install, lnmp_install, redis_setup, server_optimize, ssl_setup, wordpress
from app.services.deploy_cancel import DeployCancelledError, is_deploy_cancelled
from app.services.deploy_lock import (
    acquire_deploy_lock,
    acquire_host_lock,
    refresh_deploy_lock,
    release_deploy_lock,
    release_host_lock,
)
from app.services.deploy_partial_result import DeployProgress, build_partial_result
from app.services.log_publisher import publish_log
from app.services.remote_probe import log_remote_state, probe_remote_state
from app.tasks.celery_app import celery_app

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
}


def _get_password(task: DeployTask) -> str:
    if not task.ssh_password_enc:
        raise RuntimeError("SSH 密码不可用，无法继续部署")
    return decrypt(task.ssh_password_enc)


def _advance_phase(db, task: DeployTask, phase: DeployPhase, note: str | None = None) -> None:
    db.refresh(task)
    task.current_phase = phase
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    label = PHASE_LABELS.get(phase, phase.value)
    message = note or f"进入阶段：{label}"
    publish_log(task.id, phase.value, message, db)


def _fail_task(db, task_id: str, message: str) -> None:
    task = db.get(DeployTask, task_id)
    if not task:
        return
    task.status = DeployStatus.FAILED
    task.error_message = message
    db.commit()
    publish_log(task_id, "system", f"部署失败: {message}", db)
    publish_log(task_id, "system", "[DONE]", db)


def _clear_ssh_password(db, task: DeployTask) -> None:
    task.ssh_password_enc = None
    db.commit()


def _abort_if_stopped(db, task_id: str) -> None:
    if is_deploy_cancelled(task_id):
        raise DeployCancelledError("用户已终止任务")
    if db.get(DeployTask, task_id) is None:
        raise DeployCancelledError("任务已删除")


def _release_db_idle(db) -> None:
    """结束只读事务，避免长时间 SSH 期间阻塞 API 启动迁移。"""
    try:
        db.commit()
    except Exception:
        db.rollback()


@celery_app.task(bind=True, name="deploy.run_pipeline")
def run_deploy_pipeline(self, task_id: str, recovery: bool = False) -> None:
    lock_owner = self.request.id or task_id
    db = SessionLocal()
    task = db.get(DeployTask, task_id)
    if not task:
        release_deploy_lock(task_id, lock_owner)
        db.close()
        return

    host = task.ssh_host
    host_port = task.ssh_port

    if not acquire_deploy_lock(task_id, lock_owner, force=recovery):
        _fail_task(db, task_id, "无法获取任务锁，可能已有 Worker 正在执行此任务")
        db.close()
        return

    if not acquire_host_lock(host, host_port, task_id, force=recovery):
        release_deploy_lock(task_id, lock_owner)
        _fail_task(db, task_id, "该服务器已有其他部署任务正在执行，请等待其完成后再试")
        db.close()
        return

    if task.status == DeployStatus.SUCCESS:
        release_host_lock(host, host_port, task_id)
        release_deploy_lock(task_id, lock_owner)
        db.close()
        return

    if is_deploy_cancelled(task_id) or db.get(DeployTask, task_id) is None:
        release_host_lock(host, host_port, task_id)
        release_deploy_lock(task_id, lock_owner)
        db.close()
        return

    progress = DeployProgress()

    try:
        _abort_if_stopped(db, task_id)
        task.status = DeployStatus.RUNNING
        task.error_message = None
        db.commit()

        password = _get_password(task)

        if recovery:
            publish_log(
                task.id,
                "system",
                "服务恢复：正在重新连接目标服务器并探测当前安装状态...",
                db,
            )
        else:
            publish_log(task.id, "system", "部署任务已开始", db)

        if task.php_version_requested and task.php_version_requested != task.php_version:
            publish_log(
                task.id,
                "system",
                f"PHP 版本：用户选择 {task.php_version_requested}，"
                f"预检已自动降级为 PHP {task.php_version}",
                db,
            )

        remote_state = probe_remote_state(task, password, db)
        progress.remote_state = remote_state
        log_remote_state(task, remote_state, db)
        _release_db_idle(db)
        refresh_deploy_lock(task_id, lock_owner)

        resume_phase = remote_state.resume_phase()
        if resume_phase != DeployPhase.STEP1_BAOTA:
            _advance_phase(
                db,
                task,
                resume_phase,
                f"续跑：根据探测结果进入「{PHASE_LABELS.get(resume_phase, resume_phase.value)}」",
            )
            refresh_deploy_lock(task_id, lock_owner)

        if recovery:
            publish_log(task.id, "system", "恢复模式：已完成步骤将跳过，进行中任务将等待，不确定的将单独重装", db)

        if remote_state.baota != "complete":
            _advance_phase(db, task, DeployPhase.STEP1_BAOTA)
            if not recovery and not task.confirm_non_fresh:
                baota_install.preflight_check(task, password, db)
            elif not recovery and task.confirm_non_fresh:
                publish_log(
                    task.id,
                    "step1_baota",
                    "非全新环境继续安装：将跳过已安装组件，从缺失步骤开始",
                    db,
                )
        _release_db_idle(db)
        panel_info = baota_install.install_baota_panel(task, password, db, remote_state)
        progress.panel_info = panel_info
        db.refresh(task)
        _release_db_idle(db)
        remote_state = probe_remote_state(task, password, db)
        progress.remote_state = remote_state
        refresh_deploy_lock(task_id, lock_owner)
        _abort_if_stopped(db, task_id)

        _advance_phase(db, task, DeployPhase.STEP2_NGINX)
        _release_db_idle(db)
        lnmp_info = lnmp_install.install_nginx(task, password, db, remote_state)
        db.refresh(task)
        refresh_deploy_lock(task_id, lock_owner)
        _abort_if_stopped(db, task_id)

        _advance_phase(db, task, DeployPhase.STEP3_PHP)
        _release_db_idle(db)
        php_info = lnmp_install.install_php_stack(task, password, db, remote_state)
        lnmp_info = {**lnmp_info, **php_info}
        progress.lnmp_info = lnmp_info
        db.refresh(task)
        refresh_deploy_lock(task_id, lock_owner)
        _abort_if_stopped(db, task_id)

        _advance_phase(db, task, DeployPhase.STEP3_MYSQL)
        _release_db_idle(db)
        mysql_info = lnmp_install.install_mysql(task, password, db, remote_state)
        lnmp_info = {**lnmp_info, **mysql_info}
        progress.lnmp_info = lnmp_info
        db.refresh(task)
        if task.mysql_version_requested and task.mysql_version_requested != task.mysql_version:
            publish_log(
                task.id,
                "system",
                f"MySQL 版本：用户选择 {task.mysql_version_requested}，"
                f"已自动降级为 MySQL {task.mysql_version}",
                db,
            )
        refresh_deploy_lock(task_id, lock_owner)
        _abort_if_stopped(db, task_id)

        _advance_phase(db, task, DeployPhase.STEP4_REDIS)
        _release_db_idle(db)
        redis_info = redis_setup.install_redis_server(task, password, db, remote_state)
        progress.redis_info = redis_info
        db.refresh(task)
        refresh_deploy_lock(task_id, lock_owner)
        _abort_if_stopped(db, task_id)

        _advance_phase(db, task, DeployPhase.STEP5_PHP_EXT)
        _release_db_idle(db)
        ext_info = redis_setup.install_php_extensions(task, password, db, remote_state)
        redis_info = {**redis_info, **ext_info}
        progress.redis_info = redis_info
        db.refresh(task)
        refresh_deploy_lock(task_id, lock_owner)
        _abort_if_stopped(db, task_id)

        _advance_phase(db, task, DeployPhase.STEP6_OPTIMIZE)
        _release_db_idle(db)
        optimize_info = server_optimize.optimize_environment(task, password, db, remote_state)
        progress.optimize_info = optimize_info
        db.refresh(task)
        refresh_deploy_lock(task_id, lock_owner)
        _abort_if_stopped(db, task_id)

        _advance_phase(db, task, DeployPhase.STEP7_SITE)
        _release_db_idle(db)
        from app.services.site_config import resolve_sites_from_task

        site_specs = resolve_sites_from_task(task)
        deployed_sites: list[dict] = []
        last_site_info: dict = {}
        last_wp_info: dict = {}
        last_ssl_info: dict = {}

        for idx, site_spec in enumerate(site_specs):
            primary = site_spec.get("primary_domain") or site_spec.get("domains", [""])[0]
            publish_log(
                task.id,
                "step7_site",
                f"正在部署网站 {idx + 1}/{len(site_specs)}: {primary}",
                db,
            )
            progress.site_info = wordpress.build_site_info_from_spec(task, site_spec)
            site_info = wordpress.setup_site_and_db(
                task, password, db, remote_state, site_spec=site_spec, site_index=idx
            )
            progress.site_info = site_info
            wp_info = wordpress.install_wordpress(task, password, site_info, db, remote_state)
            progress.wp_info = wp_info
            wp_info = wordpress.verify_site(task, password, wp_info, db)
            progress.wp_info = wp_info
            last_site_info = site_info
            last_wp_info = wp_info
            deployed_sites.append(
                {
                    "site_name": site_info.get("site_name"),
                    "site_domain": site_info.get("site_domain"),
                    "domains": site_info.get("domains", []),
                    "site_path": site_info.get("site_path"),
                    "site_url": wp_info.get("site_url"),
                    "admin_url": wp_info.get("admin_url"),
                    "admin_user": wp_info.get("admin_user"),
                    "admin_password": site_spec.get("wp_admin_password"),
                    "password_auto_generated": site_spec.get("wp_password_auto_generated", False),
                    "database": {
                        "name": site_info.get("db_name"),
                        "user": site_info.get("db_user"),
                        "password": site_info.get("db_pass"),
                        "prefix": site_info.get("db_prefix"),
                    },
                }
            )
            refresh_deploy_lock(task_id, lock_owner)
            _abort_if_stopped(db, task_id)

        _advance_phase(db, task, DeployPhase.STEP8_SSL)
        _release_db_idle(db)
        ssl_warning: str | None = None
        for idx, site_spec in enumerate(site_specs):
            site_entry = deployed_sites[idx]
            site_info = {
                "site_domain": site_entry["site_domain"],
                "site_path": site_entry["site_path"],
            }
            wp_info = {
                "site_url": site_entry["site_url"],
                "admin_url": site_entry["admin_url"],
            }
            primary = site_entry["site_domain"]
            publish_log(
                task.id,
                "step8_ssl",
                f"正在为网站 {idx + 1}/{len(site_specs)} 申请 SSL: {primary}",
                db,
            )
            try:
                ssl_info = ssl_setup.apply_baota_ssl(task, password, site_info, wp_info, db)
            except Exception as exc:
                ssl_info = {
                    "success": False,
                    "warning": ssl_setup.SSL_FAILURE_WARNING,
                    "error": str(exc),
                }
                publish_log(task.id, "step8_ssl", f"警告: {ssl_setup.SSL_FAILURE_WARNING}", db)
            if ssl_info.get("success"):
                site_entry["site_url"] = ssl_info.get("site_url", site_entry["site_url"])
                site_entry["admin_url"] = ssl_info.get("admin_url", site_entry["admin_url"])
            site_entry["ssl"] = ssl_info
            if not ssl_info.get("success"):
                ssl_warning = ssl_info.get("warning") or ssl_setup.SSL_FAILURE_WARNING
            last_ssl_info = ssl_info
            last_wp_info = {
                "site_url": site_entry["site_url"],
                "admin_url": site_entry["admin_url"],
                "admin_user": site_entry["admin_user"],
                "redis_plugin": last_wp_info.get("redis_plugin"),
            }
            progress.wp_info = last_wp_info
            refresh_deploy_lock(task_id, lock_owner)
            _abort_if_stopped(db, task_id)

        task.current_phase = DeployPhase.DONE
        task.status = DeployStatus.SUCCESS
        task.result = {
            "sites": deployed_sites,
            "site_url": last_wp_info.get("site_url"),
            "admin_url": last_wp_info.get("admin_url"),
            "site_name": task.site_name,
            "admin_user": last_wp_info.get("admin_user", task.wp_admin_user),
            "admin_password": deployed_sites[0].get("admin_password") if deployed_sites else task.wp_admin_password,
            "password_auto_generated": (
                deployed_sites[0].get("password_auto_generated", False) if deployed_sites else task.wp_password_auto_generated
            ),
            "panel_url": panel_info.get("panel_url"),
            "panel_user": task.bt_user,
            "panel_password": task.bt_password,
            "lnmp": lnmp_info,
            "optimize": optimize_info,
            "redis": redis_info,
            "redis_plugin": last_wp_info.get("redis_plugin"),
            "ssl": last_ssl_info,
            "ssl_warning": ssl_warning,
        }
        task.error_message = None
        db.commit()
        if ssl_warning:
            publish_log(task.id, "system", ssl_warning, db)
        publish_log(task.id, "system", "部署成功！您的 WordPress 网站已就绪。", db)
        publish_log(task.id, "system", "[DONE]", db)

    except DeployCancelledError:
        pass

    except Exception as e:
        db.rollback()
        task_row = db.get(DeployTask, task_id)
        if not task_row:
            pass
        elif is_deploy_cancelled(task_id) or db.get(DeployTask, task_id) is None:
            pass
        else:
            try:
                if progress.remote_state is None and task_row.ssh_password_enc:
                    pwd = decrypt(task_row.ssh_password_enc)
                    progress.remote_state = probe_remote_state(task_row, pwd, db)
            except Exception:
                pass

            partial = build_partial_result(
                task_row,
                progress,
                failed_phase=task_row.current_phase,
            )
            task_row.status = DeployStatus.FAILED
            task_row.error_message = str(e)
            task_row.result = partial if partial.get("completed_steps") else None
            db.commit()
            publish_log(task_id, "system", f"部署失败: {e}", db)
            if partial.get("manual_hint"):
                publish_log(task_id, "system", partial["manual_hint"], db)
            publish_log(task_id, "system", "[DONE]", db)

    finally:
        task_row = db.get(DeployTask, task_id)
        if task_row:
            if task_row.status == DeployStatus.SUCCESS:
                _clear_ssh_password(db, task_row)
            task_row.updated_at = datetime.utcnow()
            db.commit()
        db.close()
        release_host_lock(host, host_port, task_id)
        release_deploy_lock(task_id, lock_owner)
