from __future__ import annotations

from dataclasses import dataclass

from app.models.deploy import DeployPhase

StepStatus = str  # complete | in_progress | missing

STATUS_LABELS = {
    "complete": "已完成",
    "in_progress": "安装进行中（将等待）",
    "missing": "未完成（将执行或重装）",
}


@dataclass
class RemoteDeployState:
    baota: StepStatus
    nginx: StepStatus
    php: StepStatus
    mysql: StepStatus
    redis_server: StepStatus
    php_extensions: StepStatus
    optimize: StepStatus
    site_prepared: StepStatus
    wordpress: StepStatus

    def resume_phase(self) -> DeployPhase:
        if self.baota != "complete":
            return DeployPhase.STEP1_BAOTA
        if self.nginx != "complete":
            return DeployPhase.STEP2_NGINX
        if self.php != "complete":
            return DeployPhase.STEP3_PHP
        if self.mysql != "complete":
            return DeployPhase.STEP3_MYSQL
        if self.redis_server != "complete":
            return DeployPhase.STEP4_REDIS
        if self.php_extensions != "complete":
            return DeployPhase.STEP5_PHP_EXT
        if self.optimize != "complete":
            return DeployPhase.STEP6_OPTIMIZE
        if self.site_prepared != "complete" or self.wordpress != "complete":
            return DeployPhase.STEP7_SITE
        return DeployPhase.DONE
