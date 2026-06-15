import logging

from sqlalchemy import text

from app.core.database import Base, engine

logger = logging.getLogger(__name__)

# 避免 API 启动迁移与 Worker 长事务互相阻塞导致服务卡死
_SCHEMA_LOCK_KEY = 0x515F5F5742  # "QBWP"


def ensure_schema() -> None:
    with engine.connect() as conn:
        locked = conn.execute(
            text("SELECT pg_try_advisory_lock(:key)"),
            {"key": _SCHEMA_LOCK_KEY},
        ).scalar()
        if not locked:
            logger.warning("schema migration skipped: another process holds the lock")
            return
        try:
            conn.execute(text("SET lock_timeout = '10s'"))
            _apply_schema(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            logger.exception(
                "schema migration failed (lock timeout or concurrent DDL); "
                "continuing with existing database schema"
            )
        finally:
            try:
                conn.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": _SCHEMA_LOCK_KEY},
                )
                conn.commit()
            except Exception:
                conn.rollback()
                logger.warning("failed to release schema advisory lock", exc_info=True)


def _apply_schema(conn) -> None:
    Base.metadata.create_all(bind=conn)
    conn.execute(
        text(
            "ALTER TABLE deploy_tasks "
            "ADD COLUMN IF NOT EXISTS server_os VARCHAR(16) DEFAULT 'generic'"
        )
    )
    conn.execute(
        text("UPDATE deploy_tasks SET server_os = 'generic' WHERE server_os = 'other'")
    )
    conn.execute(
        text("ALTER TABLE deploy_tasks ALTER COLUMN server_os SET DEFAULT 'generic'")
    )
    conn.execute(
        text(
            "ALTER TABLE deploy_tasks ADD COLUMN IF NOT EXISTS confirm_non_fresh BOOLEAN DEFAULT FALSE"
        )
    )
    conn.execute(
        text("ALTER TABLE deploy_tasks ADD COLUMN IF NOT EXISTS db_prefix VARCHAR(20)")
    )
    conn.execute(
        text("ALTER TABLE deploy_tasks ADD COLUMN IF NOT EXISTS db_name VARCHAR(64)")
    )
    conn.execute(
        text("ALTER TABLE deploy_tasks ADD COLUMN IF NOT EXISTS db_user VARCHAR(32)")
    )
    conn.execute(
        text("ALTER TABLE deploy_tasks ADD COLUMN IF NOT EXISTS db_password VARCHAR(128)")
    )
    conn.execute(
        text("ALTER TABLE deploy_tasks ADD COLUMN IF NOT EXISTS site_name VARCHAR(255)")
    )
    conn.execute(
        text("ALTER TABLE deploy_tasks ADD COLUMN IF NOT EXISTS site_domain VARCHAR(255)")
    )
    conn.execute(
        text(
            "UPDATE deploy_tasks SET site_name = site_input "
            "WHERE site_name IS NULL AND site_input IS NOT NULL"
        )
    )
    conn.execute(
        text(
            "UPDATE deploy_tasks SET site_domain = site_input "
            "WHERE site_domain IS NULL AND site_input LIKE '%.%'"
        )
    )
    conn.execute(
        text("ALTER TABLE deploy_tasks ALTER COLUMN site_input DROP NOT NULL")
    )
    conn.execute(
        text(
            "ALTER TABLE deploy_tasks ADD COLUMN IF NOT EXISTS "
            "wp_password_auto_generated BOOLEAN DEFAULT FALSE"
        )
    )
    conn.execute(
        text("ALTER TABLE deploy_tasks ADD COLUMN IF NOT EXISTS sites_config JSONB")
    )
    conn.execute(
        text(
            "ALTER TABLE deploy_tasks ADD COLUMN IF NOT EXISTS "
            "php_version_requested VARCHAR(16)"
        )
    )
    conn.execute(
        text(
            "ALTER TABLE deploy_tasks ADD COLUMN IF NOT EXISTS "
            "mysql_version_requested VARCHAR(16)"
        )
    )
    # deployphase 枚举与 SQLAlchemy 写入的成员名（如 STEP2_PHP）保持一致
    for phase in (
        "STEP2_NGINX",
        "STEP3_PHP",
        "STEP2_PHP",
        "STEP3_MYSQL",
        "STEP4_REDIS",
        "STEP5_PHP_EXT",
        "STEP6_OPTIMIZE",
        "STEP7_SITE",
        "STEP8_SSL",
        # 兼容早期误加的小写值
        "step2_nginx",
        "step3_php",
        "step2_php",
        "step3_mysql",
        "step4_redis",
        "step5_php_ext",
        "step6_optimize",
        "step7_site",
        "step8_ssl",
    ):
        conn.execute(
            text(
                f"""
                DO $$ BEGIN
                    ALTER TYPE deployphase ADD VALUE '{phase}';
                EXCEPTION
                    WHEN duplicate_object THEN NULL;
                END $$;
                """
            )
        )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_deploy_stats_finished_at "
            "ON deploy_stats (finished_at)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_deploy_stats_status "
            "ON deploy_stats (status)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_deploy_stats_client_ip "
            "ON deploy_stats (client_ip)"
        )
    )
