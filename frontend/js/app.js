(function ($) {
    'use strict';

    var API_BASE = window.QBW_API_BASE || 'http://localhost:8000';
    var STORAGE_KEY = 'qbw_deploy_token';
    var LOG_CURSOR_KEY = 'qbw_deploy_log_cursor';
    var MAX_LOG_LINES = 400;
    var LOG_FETCH_LIMIT = 300;
    var deployPollTimer = null;
    var deployPollActive = false;
    var currentToken = null;
    var lastPreflight = null;
    var lastLogPhase = null;
    var lastLogId = 0;
    var seenLogIds = {};
    var DEPLOY_POLL_MS = 2000;
    var DEPLOY_POLL_REQUEST_TIMEOUT_MS = 60000;
    var CANCEL_REQUEST_TIMEOUT_MS = 60000;
    var pageGuardMode = null; // null | 'running' | 'success'
    var cancelRequestInFlight = false;
    var pendingEnvPreflight = null;

    var PHASE_STEPS = {
        step1_baota: { step: 0, label: '安装宝塔' },
        step2_nginx: { step: 1, label: '安装 Nginx' },
        step3_php: { step: 2, label: '安装 PHP' },
        step2_php: { step: 2, label: '安装 PHP' },
        step3_mysql: { step: 3, label: '安装 MySQL' },
        step4_redis: { step: 4, label: '安装 Redis' },
        step5_php_ext: { step: 5, label: '安装 PHP 组件与扩展' },
        step6_optimize: { step: 6, label: '参数调优' },
        step7_site: { step: 7, label: '创建网站并安装 WordPress' },
        step8_ssl: { step: 8, label: '申请 SSL 证书' },
        step2_lnmp: { step: 2, label: '安装 PHP' },
        step2_redis: { step: 4, label: '安装 Redis' },
        step2_optimize: { step: 6, label: '参数调优' },
        step3_site: { step: 7, label: '创建网站并安装 WordPress' },
        step4_wordpress: { step: 7, label: '创建网站并安装 WordPress' },
        step5_verify: { step: 7, label: '创建网站并安装 WordPress' },
    };

    var OS_LABELS = {
        ubuntu: 'Ubuntu',
        debian: 'Debian',
        centos: 'CentOS / RHEL 系',
        generic: '通用',
    };

    var DOMAIN_RE = /^[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?(\.[a-z]{2,})+$/i;
    var IPV4_RE = /^(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)$/;

    function isValidBindHost(host) {
        var h = (host || '').trim();
        if (!h) return false;
        if (IPV4_RE.test(h)) return true;
        if (DOMAIN_RE.test(h.toLowerCase())) return true;
        return false;
    }

    var BLOCKED_SSH_HOSTNAMES = {
        localhost: true,
        'localhost.localdomain': true,
        'ip6-localhost': true,
        'ip6-loopback': true,
    };

    function isBlockedSshHost(host) {

        var raw = (host || '').trim();
        if (!raw) return false;
        var h = raw;
        if (h.charAt(0) === '[' && h.charAt(h.length - 1) === ']') {
            h = h.slice(1, -1).trim();
        }
        var lower = h.toLowerCase().replace(/\.$/, '');
        if (BLOCKED_SSH_HOSTNAMES[lower]) return true;
        if (/^\d+$/.test(lower)) return true;
        if (lower.slice(-6) === '.local' || lower.slice(-11) === '.localhost') return true;
        if (lower === '::1' || lower === '::' || lower === '0:0:0:0:0:0:0:0' || lower === '0:0:0:0:0:0:0:1') {
            return true;
        }
        if (!IPV4_RE.test(h)) return false;
        var parts = h.split('.').map(function (p) {
            return parseInt(p, 10);
        });
        if (parts[0] === 127) return true;
        if (parts[0] === 10) return true;
        if (parts[0] === 192 && parts[1] === 168) return true;
        if (parts[0] === 172 && parts[1] >= 16 && parts[1] <= 31) return true;
        if (parts[0] === 169 && parts[1] === 254) return true;
        if (
            parts.every(function (p) {
                return p === 0;
            })
        )
            return true;
        return false;
    }

    var SSH_HOST_REJECT_MSG = '不能使用本地或内网地址（如 127.0.0.1、localhost、192.168.x.x），请填写云服务器的公网 IP 或域名';

    function $form() {
        return $('#deploy-form');
    }

    function $field(name) {
        return $form().find('[name="' + name + '"]');
    }

    function getTrimmedField(name) {
        return ($field(name).val() || '').trim();
    }

    function validateSshHost() {
        var sshHost = getTrimmedField('ssh_host');
        if (!sshHost) {
            toast('请填写 SSH 地址', 'error');
            return false;
        }
        if (isBlockedSshHost(sshHost)) {
            toast(SSH_HOST_REJECT_MSG, 'error');
            return false;
        }
        return true;
    }

    function parseAjaxErrorMessage(xhr, fallback) {
        var msg = fallback || '请求失败';
        try {
            var err = xhr && xhr.responseJSON;
            if (!err || !err.detail) return msg;
            if (typeof err.detail === 'string') return err.detail;
            if (Array.isArray(err.detail) && err.detail.length) {
                var first = err.detail[0];
                if (typeof first === 'string') return first;
                if (first && first.msg) {
                    return String(first.msg).replace(/^Value error,\s*/i, '');
                }
            }
        } catch (e) {
            /* ignore */
        }
        return msg;
    }

    var LOCALE_LABELS = {
        zh_CN: '简体中文',
        en_US: 'English',
    };

    function toast(msg, type) {
        var $t = $('#toast');
        $t.text(msg).removeClass('error success hidden');
        if (type) $t.addClass(type);
        setTimeout(function () {
            $t.addClass('hidden');
        }, 4000);
    }

    function esc(text) {
        return $('<div>')
            .text(text == null ? '' : String(text))
            .html();
    }

    function maskSecret(value, placeholder) {
        return value ? '••••••••' : placeholder || '（自动生成）';
    }

    function saveDeployToken(token) {
        try {
            localStorage.setItem(STORAGE_KEY, token);
        } catch (e) {
            /* ignore */
        }
    }

    function loadDeployToken() {
        try {
            return localStorage.getItem(STORAGE_KEY);
        } catch (e) {
            return null;
        }
    }

    function clearDeployToken() {
        try {
            localStorage.removeItem(STORAGE_KEY);
            localStorage.removeItem(LOG_CURSOR_KEY);
        } catch (e) {
            /* ignore */
        }
    }

    function saveLogCursor(token, logId) {
        if (!token || !logId) return;
        try {
            var data = {};
            try {
                data = JSON.parse(localStorage.getItem(LOG_CURSOR_KEY) || '{}');
            } catch (e2) {
                /* ignore */
            }
            data[token] = logId;
            localStorage.setItem(LOG_CURSOR_KEY, JSON.stringify(data));
        } catch (e) {
            /* ignore */
        }
    }

    function loadLogCursor(token) {
        if (!token) return 0;
        try {
            var data = JSON.parse(localStorage.getItem(LOG_CURSOR_KEY) || '{}');
            return parseInt(data[token], 10) || 0;
        } catch (e) {
            return 0;
        }
    }

    function sshPayload() {
        return {
            ssh_host: getTrimmedField('ssh_host'),
            ssh_password: $field('ssh_password').val(),
            ssh_port: parseInt($field('ssh_port').val(), 10) || 22,
            ssh_user: getTrimmedField('ssh_user') || 'root',
            server_os: $field('server_os').val() || 'generic',
        };
    }

    function parseDomainsText(text) {
        var domains = [];
        var seen = {};
        (text || '').split('\n').forEach(function (line) {
            var d = line.trim().toLowerCase();
            if (!d || seen[d]) return;
            seen[d] = true;
            domains.push(d);
        });
        return domains;
    }

    function refreshSiteCardIndexes() {
        $('#sites-container .site-card').each(function (index) {
            $(this)
                .find('.site-index')
                .text(index + 1);
            var $remove = $(this).find('.btn-remove-site');
            if ($('#sites-container .site-card').length <= 1) {
                $remove.prop('disabled', true).addClass('disabled');
            } else {
                $remove.prop('disabled', false).removeClass('disabled');
            }
        });
    }

    function addSiteCard() {
        var tpl = document.getElementById('site-card-template');
        if (!tpl || !tpl.content) return;
        var node = tpl.content.cloneNode(true);
        $('#sites-container').append(node);
        refreshSiteCardIndexes();
    }

    function collectSitesFromForm() {
        var sites = [];
        $('#sites-container .site-card').each(function () {
            var $card = $(this);
            var domains = parseDomainsText($card.find('.site-domains').val());
            var site = { domains: domains, wp_locale: $card.find('.wp-locale').val() || 'zh_CN' };
            var siteName = $card.find('.site-name').val().trim();
            var wpUser = $card.find('.wp-admin-user').val().trim();
            var wpPass = $card.find('.wp-admin-password').val();
            var email = $card.find('.wp-admin-email').val().trim();
            var dbPrefix = $card.find('.db-prefix').val().trim();
            var dbName = $card.find('.db-name').val().trim();
            var dbUser = $card.find('.db-user').val().trim();
            var dbPass = $card.find('.db-password').val();
            if (siteName) site.site_name = siteName;
            if (wpUser) site.wp_admin_user = wpUser;
            if (wpPass) site.wp_admin_password = wpPass;
            if (email) site.wp_admin_email = email;
            if (dbPrefix) site.db_prefix = dbPrefix;
            if (dbName) site.db_name = dbName;
            if (dbUser) site.db_user = dbUser;
            if (dbPass) site.db_password = dbPass;
            sites.push(site);
        });
        return sites;
    }

    function collectAllDomainsFromSites(sites) {
        var all = [];
        var seen = {};
        sites.forEach(function (site) {
            (site.domains || []).forEach(function (d) {
                if (!seen[d]) {
                    seen[d] = true;
                    all.push(d);
                }
            });
        });
        return all;
    }

    function collectFormData(extra) {
        var sites = collectSitesFromForm();
        var data = {
            ssh_host: getTrimmedField('ssh_host'),
            ssh_password: $field('ssh_password').val(),
            ssh_port: parseInt($field('ssh_port').val(), 10) || 22,
            ssh_user: getTrimmedField('ssh_user') || 'root',
            server_os: $field('server_os').val() || 'generic',
            confirm_non_fresh: false,
            bt_port: parseInt($field('bt_port').val(), 10) || 8888,
            nginx_version: $field('nginx_version').val(),
            php_version: $field('php_version').val(),
            mysql_version: $field('mysql_version').val(),
            sites: sites,
        };
        var btUser = getTrimmedField('bt_user');
        var btPass = $field('bt_password').val();
        if (btUser) data.bt_user = btUser;
        if (btPass) data.bt_password = btPass;
        if (extra) $.extend(data, extra);
        return data;
    }

    function validateForm() {
        var form = document.getElementById('deploy-form');
        if (!form.checkValidity()) {
            form.reportValidity();
            return false;
        }
        if (!validateSshHost()) {
            return false;
        }
        var sites = collectSitesFromForm();
        if (!sites.length) {
            toast('请至少添加一个网站', 'error');
            return false;
        }
        var allDomains = [];
        var domainSeen = {};
        for (var i = 0; i < sites.length; i++) {
            var site = sites[i];
            var cardNum = i + 1;
            if (!site.domains || !site.domains.length) {
                toast('网站 ' + cardNum + '：请至少填写一个绑定域名', 'error');
                return false;
            }
            for (var j = 0; j < site.domains.length; j++) {
                var d = site.domains[j];
                if (!isValidBindHost(d)) {
                    toast('网站 ' + cardNum + '：「' + d + '」不是有效的域名或 IP', 'error');
                    return false;
                }
                if (domainSeen[d]) {
                    toast('域名「' + d + '」在多个网站中重复，请检查', 'error');
                    return false;
                }
                domainSeen[d] = true;
                allDomains.push(d);
            }
            var $card = $('#sites-container .site-card').eq(i);
            var user = $card.find('.wp-admin-user').val().trim();
            if (user && !/^[a-zA-Z0-9_\-]+$/.test(user)) {
                toast('网站 ' + cardNum + '：管理员账号只能包含字母、数字、下划线和连字符', 'error');
                return false;
            }
            var pass = $card.find('.wp-admin-password').val();
            if (pass && pass.length < 6) {
                toast('网站 ' + cardNum + '：管理员密码至少 6 位', 'error');
                return false;
            }
            var dbPass = $card.find('.db-password').val();
            if (dbPass && dbPass.length < 6) {
                toast('网站 ' + cardNum + '：数据库密码至少 6 位', 'error');
                return false;
            }
        }
        return true;
    }

    var WEB_ENV_LABELS = {
        nginx: 'Nginx',
        apache: 'Apache',
        php: 'PHP',
        mysql: 'MySQL/MariaDB',
        'nginx-bin': '系统 Nginx',
        'httpd-bin': '系统 Httpd',
        'apache2-bin': '系统 Apache2',
    };

    function formatWebEnvironment(list) {
        if (!list || !list.length) return '无';
        var seen = {};
        return list
            .map(function (key) {
                var label = WEB_ENV_LABELS[key] || key;
                return seen[label] ? null : (seen[label] = label);
            })
            .filter(Boolean)
            .join('、');
    }

    function renderPreflightResult(res) {
        lastPreflight = res;
        var $box = $('#preflight-result');
        $box.removeClass('hidden ok-fresh warn-env error');

        if (res.blocked || res.domain_conflict) {
            $box.addClass('error').html('<strong>无法继续安装</strong><p>' + esc(res.message) + '</p>' + buildEnvDetailHtml(res));
            return;
        }

        if (!res.ok) {
            $box.addClass('error').html('<strong>检测未通过</strong><p>' + esc(res.message) + '</p>');
            return;
        }

        var html = '<strong>' + (res.is_fresh ? '✓ 全新环境，可以部署' : '⚠ 检测到非全新痕迹') + '</strong>';
        html += '<p>' + esc(res.message) + '</p>';
        html += '<ul>';
        html += '<li>SSH 连通：成功</li>';
        html += '<li>系统：' + esc((res.os_pretty || res.os_detected) + ' ' + (res.os_version || '')) + '</li>';
        html += '<li>宝塔面板：' + (res.baota_installed ? '已安装' : '未安装') + '</li>';
        html += '<li>Web 环境：' + esc(formatWebEnvironment(res.web_environment)) + '</li>';
        if (res.site_dirs > 0) {
            html += '<li>网站目录：' + res.site_dirs + ' 个</li>';
        }
        html += '</ul>';

        if (res.warnings && res.warnings.length) {
            html += '<p class="text-danger"><strong>提示：</strong></p><ul>';
            res.warnings.forEach(function (w) {
                html += '<li class="text-danger">' + esc(w) + '</li>';
            });
            html += '</ul>';
        }

        $box.addClass(res.is_fresh ? 'ok-fresh' : 'warn-env').html(html);
    }

    function runPreflight() {
        var payload = sshPayload();
        if (!payload.ssh_host || !payload.ssh_password) {
            toast('请先填写 SSH 地址和密码', 'error');
            return $.Deferred().reject();
        }
        if (!validateSshHost()) {
            return $.Deferred().reject();
        }
        payload.site_domains = collectAllDomainsFromSites(collectSitesFromForm());
        if (payload.site_domains.length) {
            payload.site_domain = payload.site_domains[0];
        }
        payload.php_version = $field('php_version').val();
        return $.ajax({
            url: API_BASE + '/api/deploy/preflight',
            method: 'POST',
            contentType: 'application/json',
            data: JSON.stringify(payload),
            dataType: 'json',
            timeout: 90000,
        });
    }

    function buildEnvDetailHtml(res) {
        var html = '<ul>';
        html += '<li>操作系统：' + esc((res.os_pretty || res.os_detected || '') + ' ' + (res.os_version || '')) + '</li>';
        if (res.php_version_fallback && res.php_version_requested && res.php_version_effective) {
            html += '<li>PHP 版本：' + esc(res.php_version_requested) + ' → 将使用 PHP ' + esc(res.php_version_effective) + '（系统自动降级）</li>';
        }
        html += '<li>宝塔面板：' + (res.baota_installed ? '已安装' : '未安装') + '</li>';
        html += '<li>Web 环境：' + esc(formatWebEnvironment(res.web_environment)) + '</li>';
        if (res.site_dirs > 0) {
            html += '<li>网站目录：已检测到 ' + res.site_dirs + ' 个';
            if (res.site_samples && res.site_samples.length) {
                html += '（如 ' + esc(res.site_samples.slice(0, 3).join('、')) + '）';
            }
            html += '</li>';
        }
        var planDomains = res.target_domains && res.target_domains.length ? res.target_domains : res.target_domain ? [res.target_domain] : [];
        if (planDomains.length) {
            html +=
                '<li>计划绑定域名：' +
                planDomains
                    .map(function (d) {
                        return '<code>' + esc(d) + '</code>';
                    })
                    .join('、') +
                '</li>';
        }
        if (res.conflicting_domains && res.conflicting_domains.length) {
            html += '<li class="text-danger">以下域名对应的网站已存在：' + esc(res.conflicting_domains.join('、')) + '</li>';
        } else if (res.existing_site_for_domain) {
            html += '<li class="text-danger">部分计划域名对应的网站目录或站点配置已存在</li>';
        }
        if (res.warnings && res.warnings.length) {
            html += '</ul><p class="text-danger" style="margin:12px 0 4px"><strong>详细提示：</strong></p><ul>';
            res.warnings.forEach(function (w) {
                html += '<li class="text-danger">' + esc(w) + '</li>';
            });
        }
        html += '</ul>';
        return html;
    }

    function showEnvModal(id) {
        $('.modal').addClass('hidden');
        $('#' + id).removeClass('hidden');
    }

    function hideEnvModals() {
        $('.modal').addClass('hidden');
        pendingEnvPreflight = null;
    }

    function showEnvBlockedModal(res) {
        var body = '<p>' + esc(res.message || '当前环境不允许继续安装。') + '</p>';
        body += buildEnvDetailHtml(res);
        $('#env-blocked-body').html(body);
        showEnvModal('env-blocked-modal');
    }

    function showEnvWarningModal(res) {
        pendingEnvPreflight = res;
        $('#env-warning-body').html(buildEnvDetailHtml(res));
        showEnvModal('env-warning-modal');
    }

    function handlePreflightForDeploy(res) {
        if (!res || !res.ssh_ok) {
            toast(res && res.message ? res.message : 'SSH 连接失败', 'error');
            return false;
        }
        if (res.blocked || res.domain_conflict) {
            showEnvBlockedModal(res);
            return false;
        }
        if (!res.ok) {
            toast(res.message || '环境检测未通过', 'error');
            return false;
        }
        if (!res.is_fresh) {
            showEnvWarningModal(res);
            return false;
        }
        enterConfirmPage(res);
        return true;
    }

    function renderCheckFlow(res) {
        var osText = (res.os_pretty || res.os_detected || 'Linux') + (res.os_version ? ' ' + res.os_version : '');
        var envDetail;
        if (res.is_fresh) {
            envDetail = '未检测到宝塔与 Web 环境';
        } else {
            var parts = [];
            if (res.baota_installed) parts.push('宝塔已安装');
            if (res.web_environment && res.web_environment.length) {
                parts.push(formatWebEnvironment(res.web_environment));
            }
            if (res.site_dirs > 0) parts.push(res.site_dirs + ' 个网站目录');
            envDetail = '非全新环境：' + (parts.length ? parts.join('、') : '存在安装痕迹');
            envDetail += '；将自动跳过已装组件并从缺失步骤续装';
        }
        if (!res.os_match) {
            envDetail += '；系统类型与所选不一致，将按所选脚本安装';
        }

        var items = [
            { label: 'SSH 连接', status: '成功', detail: getTrimmedField('ssh_host'), state: 'done' },
            { label: '操作系统', status: '已识别', detail: osText.trim(), state: 'done' },
            { label: '环境检测', status: '通过', detail: envDetail, state: 'done' },
        ];

        var html = '';
        items.forEach(function (item, index) {
            html += '<div class="check-item ' + item.state + '">';
            html += '<div class="check-icon">' + (item.state === 'done' ? '✓' : index + 1) + '</div>';
            html += '<div class="check-body">';
            html += '<div class="check-head"><span class="check-label">' + esc(item.label) + '</span>';
            html += '<span class="check-status">' + esc(item.status) + '</span></div>';
            html += '<div class="check-detail">' + esc(item.detail) + '</div>';
            html += '</div></div>';
        });
        $('#check-flow').html(html);
    }

    function buildConfirmRows() {
        var nginxVal = $field('nginx_version').val();
        var phpVal = $field('php_version').val();
        var mysqlVal = $field('mysql_version').val();
        var rows = [
            ['SSH 地址', getTrimmedField('ssh_host')],
            ['SSH 端口', String(parseInt($field('ssh_port').val(), 10) || 22)],
            ['SSH 用户名', getTrimmedField('ssh_user') || 'root'],
            ['SSH 密码', maskSecret($field('ssh_password').val())],
            ['系统类型', OS_LABELS[$field('server_os').val()] || $field('server_os').val()],
            ['宝塔用户名', getTrimmedField('bt_user') || '（自动生成）'],
            ['宝塔密码', maskSecret($field('bt_password').val())],
            ['宝塔端口', String(parseInt($field('bt_port').val(), 10) || 8888)],
            ['Web 服务器', 'Nginx ' + nginxVal],
            ['PHP 版本', 'PHP ' + phpVal],
            ['数据库', 'MySQL ' + mysqlVal],
            ['网站数量', String(collectSitesFromForm().length) + ' 个'],
        ];
        collectSitesFromForm().forEach(function (site, index) {
            var n = index + 1;
            rows.push(['网站 ' + n + ' 名称', site.site_name || '示例博客（默认）']);
            rows.push(['网站 ' + n + ' 域名', (site.domains || []).join('、')]);
            rows.push(['网站 ' + n + ' 管理员', site.wp_admin_user || 'admin（默认）']);
            rows.push(['网站 ' + n + ' 管理员密码', site.wp_admin_password ? maskSecret(site.wp_admin_password) : '（自动生成）']);
            rows.push(['网站 ' + n + ' 语言', LOCALE_LABELS[site.wp_locale] || site.wp_locale]);
        });
        return rows;
    }

    function renderConfirmTable() {
        var html = '';
        buildConfirmRows().forEach(function (row) {
            html += '<tr><th>' + esc(row[0]) + '</th><td>' + esc(row[1]) + '</td></tr>';
        });
        $('#confirm-table').html(html);
    }

    function enterConfirmPage(res) {
        lastPreflight = res;
        $('#form-section, #page-alert').addClass('hidden');
        $('#progress-section').removeClass('hidden');
        $('#progress-title').text('确认部署');
        $('#confirm-panel').removeClass('hidden');
        $('#deploy-panel').addClass('hidden');
        renderCheckFlow(res);
        renderConfirmTable();
        window.scrollTo({ top: 0, behavior: 'smooth' });
    }

    function backToForm() {
        $('#progress-section').addClass('hidden');
        $('#confirm-panel, #deploy-panel').addClass('hidden');
        $('#form-section, #page-alert').removeClass('hidden');
        $('#progress-title').text('确认部署');
        window.scrollTo({ top: 0, behavior: 'smooth' });
    }

    function setPageGuard(mode) {
        pageGuardMode = mode;
    }

    function resetToNewDeploy() {
        clearDeployToken();
        stopDeployPolling();
        setPageGuard(null);
        currentToken = null;
        lastLogPhase = null;
        lastLogId = 0;
        seenLogIds = {};
        $('#deploy-panel').removeClass('deploy-panel--success');
        $('#log-section').removeClass('hidden');
    }

    function showDeployPanel() {
        $('#progress-title').text('部署进度');
        $('#confirm-panel').addClass('hidden');
        $('#deploy-panel').removeClass('hidden deploy-panel--success');
        $('#result-success, #result-failed').addClass('hidden');
        $('#result-ssl-notice').addClass('hidden').empty();
        hidePartialFailureSections();
        $('#deploy-toolbar').removeClass('hidden');
        $('#log-section').removeClass('hidden');
        $('#log-viewer').html('<div class="log-placeholder" style="color:#888">等待日志...</div>');
        $('#progress-hint').text('预计 20–40 分钟，请勿关闭页面');
    }

    function setDeployToolbarVisible(visible) {
        $('#deploy-toolbar').toggleClass('hidden', !visible);
    }

    function pauseDeployPolling() {
        stopDeployPolling();
    }

    function resumeDeployPolling(token) {
        if (!token || currentToken !== token) return;
        pollDeployOnce(token, function () {
            if (currentToken !== token) return;
            startDeployPolling(token);
        });
    }

    function requestTaskCancel(token) {
        return $.ajax({
            url: API_BASE + '/api/deploy/' + token + '/cancel',
            method: 'POST',
            dataType: 'json',
            timeout: CANCEL_REQUEST_TIMEOUT_MS,
        });
    }

    function applyCancelSuccess(res) {
        resetToNewDeploy();
        backToForm();
        toast((res && res.message) || '任务已终止', 'success');
    }

    function cancelDeployButtons() {
        return $('#btn-cancel-deploy, #btn-cancel-failed');
    }

    function cancelDeploy() {
        var token = currentToken || loadDeployToken();
        if (!token || cancelRequestInFlight) return;

        var message = '确定要终止当前部署任务吗？\n\n' + '终止后，目标服务器上可能留有未完成的安装残留（如宝塔、PHP、数据库等）。' + '建议重新安装系统后再进行部署。\n\n' + '是否确认终止？';
        if (!window.confirm(message)) return;

        cancelRequestInFlight = true;
        var $btns = cancelDeployButtons().prop('disabled', true);
        $btns.filter('#btn-cancel-deploy').text('正在终止...');
        $btns.filter('#btn-cancel-failed').text('正在终止...');
        pauseDeployPolling();

        requestTaskCancel(token)
            .done(function (res) {
                if (!res || res.ok !== true) {
                    toast('终止失败，请稍后重试', 'error');
                    resumeDeployPolling(token);
                    return;
                }
                applyCancelSuccess(res);
            })
            .fail(function (xhr) {
                var msg = '终止失败，请稍后重试';
                try {
                    var err = xhr.responseJSON;
                    if (err.detail) msg = typeof err.detail === 'string' ? err.detail : msg;
                } catch (e2) {
                    /* ignore */
                }
                toast(msg, 'error');
                resumeDeployPolling(token);
            })
            .always(function () {
                cancelRequestInFlight = false;
                var $btns = cancelDeployButtons().prop('disabled', false);
                $btns.filter('#btn-cancel-deploy').text('终止任务');
                $btns.filter('#btn-cancel-failed').text('终止任务');
            });
    }

    function submitDeploy() {
        var needConfirm = lastPreflight && !lastPreflight.is_fresh;
        var $btn = $('#btn-confirm-start').prop('disabled', true).text('正在验证并提交...');
        showDeployPanel();

        $.ajax({
            url: API_BASE + '/api/deploy',
            method: 'POST',
            contentType: 'application/json',
            data: JSON.stringify(collectFormData({ confirm_non_fresh: !!needConfirm })),
            dataType: 'json',
            timeout: 120000,
        })
            .done(function (res) {
                toast('部署任务已启动', 'success');
                showProgress(res.token, false);
            })
            .fail(function (xhr, status) {
                var msg = status === 'timeout' ? '提交超时，服务器环境验证耗时过长，请稍后重试' : '提交失败';
                toast(parseAjaxErrorMessage(xhr, msg), 'error');
                $('#confirm-panel').removeClass('hidden');
                $('#deploy-panel').addClass('hidden');
                $('#progress-title').text('确认部署');
            })
            .always(function () {
                $btn.prop('disabled', false).text('确认并开始部署');
            });
    }

    function updateSteps(userStep, status) {
        var isFailed = status === 'failed';
        var isSuccess = status === 'success';

        $('.step').each(function () {
            var $s = $(this);
            var idx = parseInt($s.data('step'), 10);
            var $num = $s.find('.step-num');
            $s.removeClass('active done error');
            $num.text(idx + 1);

            if (isSuccess || idx < userStep) {
                $s.addClass('done');
                $num.text('✓');
            } else if (isFailed && idx === userStep) {
                $s.addClass('error');
                $num.text('!');
            } else if (idx === userStep && !isSuccess && !isFailed) {
                $s.addClass('active');
            }
        });
    }

    function stopDeployPolling() {
        deployPollActive = false;
        if (deployPollTimer) {
            clearTimeout(deployPollTimer);
            deployPollTimer = null;
        }
    }

    function scheduleDeployPollLoop(token) {
        if (!deployPollActive || currentToken !== token) return;
        deployPollTimer = setTimeout(function () {
            deployPollTimer = null;
            runDeployPollLoop(token);
        }, DEPLOY_POLL_MS);
    }

    function runDeployPollLoop(token) {
        if (!deployPollActive || currentToken !== token) return;

        pollDeployOnce(token, function (res) {
            if (!deployPollActive || currentToken !== token) return;
            if (res && res.done) {
                stopDeployPolling();
                return;
            }
            scheduleDeployPollLoop(token);
        });
    }

    function applyDeployStatus(data) {
        updateSteps(data.user_step, data.status);
        if (data.user_step_label && data.status !== 'success' && data.status !== 'failed') {
            $('#current-step-label').text('当前正在进行：' + data.user_step_label);
        } else {
            $('#current-step-label').text('');
        }

        setDeployToolbarVisible(data.status === 'running' || data.status === 'pending');

        if (data.status === 'success' && data.result) {
            stopDeployPolling();
            setDeployToolbarVisible(false);
            renderResult(data.result);
        } else if (data.status === 'failed') {
            stopDeployPolling();
            setDeployToolbarVisible(false);
            setPageGuard(null);
            $('#deploy-panel').removeClass('deploy-panel--success');
            $('#result-success').addClass('hidden');
            renderFailedResult(data.error_message, data.result);
            $('#result-failed').removeClass('hidden');
            $('#progress-hint').text('部署失败');
            $('#progress-title').text('部署未完成');
        } else if (data.status === 'running' || data.status === 'pending') {
            setPageGuard('running');
        }
    }

    function applyPhaseProgress(phase) {
        var info = PHASE_STEPS[phase];
        if (!info) return;
        updateSteps(info.step, 'running');
        $('#current-step-label').text('当前正在进行：' + info.label);
    }

    function applyStatusSnapshot(data) {
        if (!data || data.status === undefined) return;
        applyDeployStatus(data);
        if (data.current_phase && data.current_phase !== lastLogPhase && data.current_phase !== 'system') {
            lastLogPhase = data.current_phase;
            applyPhaseProgress(data.current_phase);
        }
    }

    function ingestLogEntry(token, entry) {
        if (!entry || !entry.message) return;
        if (entry.id) {
            if (seenLogIds[entry.id]) return;
            seenLogIds[entry.id] = true;
            if (entry.id > lastLogId) {
                lastLogId = entry.id;
                saveLogCursor(token, lastLogId);
            }
        }
        if (entry.message === '[DONE]') {
            return;
        }
        appendLog(entry.phase, entry.message);
        if (entry.phase && entry.phase !== 'system') {
            if (entry.phase !== lastLogPhase) {
                lastLogPhase = entry.phase;
                applyPhaseProgress(entry.phase);
            }
        }
        if (entry.phase === 'system') {
            var resumeMatch = entry.message.match(/将从步骤「(.+?)」继续执行/);
            if (resumeMatch) {
                var label = resumeMatch[1];
                Object.keys(PHASE_STEPS).some(function (phase) {
                    if (PHASE_STEPS[phase].label === label) {
                        lastLogPhase = phase;
                        applyPhaseProgress(phase);
                        return true;
                    }
                    return false;
                });
            }
        }
    }

    function pollDeployOnce(token, callback, catchUpRound) {
        if (cancelRequestInFlight) {
            if (callback) callback(null);
            return;
        }
        catchUpRound = catchUpRound || 0;
        $.ajax({
            url: API_BASE + '/api/deploy/' + token + '/logs/tail',
            method: 'GET',
            data: { after_id: lastLogId, limit: LOG_FETCH_LIMIT },
            dataType: 'json',
            timeout: DEPLOY_POLL_REQUEST_TIMEOUT_MS,
        })
            .done(function (res) {
                if (res.truncated && res.skipped > 0 && catchUpRound === 0) {
                    appendLog('system', '（已省略较早的 ' + res.skipped + ' 条日志，仅显示最近 ' + LOG_FETCH_LIMIT + ' 条）');
                }
                (res.logs || []).forEach(function (entry) {
                    ingestLogEntry(token, entry);
                });
                applyStatusSnapshot(res);
                var batchSize = (res.logs || []).length;
                if (batchSize >= LOG_FETCH_LIMIT && catchUpRound < 8) {
                    pollDeployOnce(token, callback, catchUpRound + 1);
                    return;
                }
                if (res.done) {
                    stopDeployPolling();
                }
                if (callback) callback(res);
            })
            .fail(function () {
                if (callback) callback(null);
            });
    }

    function startDeployPolling(token) {
        stopDeployPolling();
        deployPollActive = true;
        scheduleDeployPollLoop(token);
    }

    function appendLog(phase, message) {
        if (message === '[DONE]') return;
        if (!message || !String(message).trim()) return;
        var $viewer = $('#log-viewer');
        if ($viewer.find('.log-placeholder').length) {
            $viewer.empty();
        }
        $viewer.append('<div class="log-line"><span class="phase">[' + esc(phase) + ']</span> ' + esc(message) + '</div>');
        var $lines = $viewer.children('.log-line');
        if ($lines.length > MAX_LOG_LINES) {
            $lines.slice(0, $lines.length - MAX_LOG_LINES).remove();
        }
        $viewer.scrollTop($viewer[0].scrollHeight);
    }

    function showProgressLayout(token) {
        currentToken = token;
        saveDeployToken(token);
        $('#task-link').text(token);
        $('#progress-title').text('部署进度');
        $('#result-success, #result-failed').addClass('hidden');
        $('#result-ssl-notice').addClass('hidden').empty();
        hidePartialFailureSections();
        $('#log-viewer').html('<div class="log-placeholder" style="color:#888">加载日志...</div>');
        lastLogPhase = null;
        lastLogId = loadLogCursor(token);
        seenLogIds = {};
        stopDeployPolling();
    }

    function bootstrapProgress(token, switchLayout) {
        if (switchLayout !== false) {
            $('#form-section, #page-alert').addClass('hidden');
            $('#progress-section').removeClass('hidden');
            showDeployPanel();
        }

        showProgressLayout(token);
        setPageGuard('running');

        pollDeployOnce(token, function (res) {
            if (currentToken !== token) return;
            if (res && !res.done) {
                startDeployPolling(token);
            }
        });
    }

    function showProgress(token, switchLayout) {
        bootstrapProgress(token, switchLayout);
    }

    function resumeDeployMonitoring(token) {
        stopDeployPolling();
        currentToken = token;
        $('#result-failed, #result-success').addClass('hidden');
        hidePartialFailureSections();
        $('#deploy-panel').removeClass('deploy-panel--success');
        $('#progress-title').text('部署进度');
        $('#progress-hint').text('正在重新执行，请勿关闭页面');
        $('#current-step-label').text('');
        setDeployToolbarVisible(true);
        setPageGuard('running');

        pollDeployOnce(token, function (res) {
            if (currentToken !== token) return;
            if (res && !res.done) {
                startDeployPolling(token);
            }
        });
    }

    function retryDeploy() {
        var token = currentToken || loadDeployToken();
        if (!token || cancelRequestInFlight) return;

        var $btn = $('#btn-retry').prop('disabled', true).text('正在重试...');
        $.ajax({
            url: API_BASE + '/api/deploy/' + token + '/retry',
            method: 'POST',
            dataType: 'json',
        })
            .done(function (res) {
                if (!res || res.ok !== true) {
                    toast('重试失败，请稍后重试', 'error');
                    return;
                }
                resumeDeployMonitoring(token);
                if (res.user_step_label) {
                    $('#current-step-label').text('正在重试：' + res.user_step_label);
                }
                toast(res.message || '已开始重试', 'success');
            })
            .fail(function (xhr) {
                var msg = '重试失败，请稍后重试';
                try {
                    var err = xhr.responseJSON;
                    if (err.detail) msg = typeof err.detail === 'string' ? err.detail : msg;
                } catch (e2) {
                    /* ignore */
                }
                toast(msg, 'error');
            })
            .always(function () {
                $btn.prop('disabled', false).text('立即重试');
            });
    }

    function buildResultTableRows(rows) {
        var html = '';
        rows.forEach(function (r) {
            if (r[1] === undefined || r[1] === null || r[1] === '') return;
            var isLink = r[2] === true;
            var val = isLink ? '<a href="' + esc(r[1]) + '" target="_blank" rel="noreferrer">' + esc(r[1]) + '</a>' : esc(r[1]);
            html += '<tr><th>' + esc(r[0]) + '</th><td>' + val + '</td></tr>';
        });
        return html;
    }

    function hidePartialFailureSections() {
        $('#partial-manual-hint, #partial-completed-section, #partial-save-hint').addClass('hidden');
        $('#partial-baota-section, #partial-env-section, #partial-db-section, #partial-site-section').addClass('hidden');
        $('#partial-completed-list, #partial-table-baota, #partial-table-env, #partial-table-db, #partial-table-site').empty();
    }

    function renderFailedResult(errorMessage, result) {
        hidePartialFailureSections();
        var errText = errorMessage || '未知错误';
        if (result && result.partial && result.failed_label) {
            errText = '在「' + result.failed_label + '」步骤失败：' + errText;
        }
        $('#error-message').text(errText);

        if (!result || !result.partial) return;

        if (result.manual_hint) {
            $('#partial-manual-hint').removeClass('hidden').text(result.manual_hint);
        }

        if (result.completed_steps && result.completed_steps.length) {
            var listHtml = '';
            result.completed_steps.forEach(function (step) {
                listHtml += '<li>' + esc(step.label) + '</li>';
            });
            $('#partial-completed-list').html(listHtml);
            $('#partial-completed-section').removeClass('hidden');
        }

        var hasCreds = result.panel_url || result.environment || result.database || result.site;
        if (hasCreds) {
            $('#partial-save-hint').removeClass('hidden');
        }

        if (result.panel_url) {
            $('#partial-table-baota').html(
                buildResultTableRows([
                    ['面板地址', result.panel_url, true],
                    ['登录账号', result.panel_user],
                    ['登录密码', result.panel_password],
                ])
            );
            $('#partial-baota-section').removeClass('hidden');
        }

        if (result.environment) {
            var env = result.environment;
            var envRows = [];
            if (env.nginx) envRows.push(['Nginx 版本', env.nginx]);
            if (env.php) envRows.push(['PHP 版本', env.php]);
            if (env.mysql) envRows.push(['MySQL 版本', env.mysql]);
            if (envRows.length) {
                $('#partial-table-env').html(buildResultTableRows(envRows));
                $('#partial-env-section').removeClass('hidden');
            }
        }

        if (result.database && result.database.name) {
            $('#partial-table-db').html(
                buildResultTableRows([
                    ['数据库名', result.database.name],
                    ['数据库用户', result.database.user],
                    ['数据库密码', result.database.password],
                    ['表前缀', result.database.prefix],
                ])
            );
            $('#partial-db-section').removeClass('hidden');
        }

        var siteRows = [];
        if (result.site) {
            if (result.site.site_name) siteRows.push(['网站名称', result.site.site_name]);
            if (result.site.site_domain) siteRows.push(['域名', result.site.site_domain]);
            if (result.site.site_path) siteRows.push(['站点目录', result.site.site_path]);
            if (result.site.site_url) siteRows.push(['网站地址', result.site.site_url, true]);
        }
        if (result.admin_url) siteRows.push(['后台地址', result.admin_url, true]);
        if (result.admin_user) siteRows.push(['管理员账号', result.admin_user]);
        if (result.admin_password) siteRows.push(['管理员密码', result.admin_password]);
        if (siteRows.length) {
            $('#partial-table-site').html(buildResultTableRows(siteRows));
            $('#partial-site-section').removeClass('hidden');
        }
    }

    var SSL_FAILURE_WARNING = '证书申请失败了，请在宝塔手动申请证书即可';

    function renderResult(result) {
        var baotaRows = [
            ['面板地址', result.panel_url, true],
            ['登录账号', result.panel_user],
            ['登录密码', result.panel_password],
        ];
        var siteRows = [];
        var hasAutoPassword = false;
        var sslWarning = result.ssl_warning || null;

        if (result.sites && result.sites.length) {
            result.sites.forEach(function (site, index) {
                var n = index + 1;
                var prefix = result.sites.length > 1 ? '网站 ' + n + ' ' : '';
                siteRows.push([prefix + '名称', site.site_name || '—']);
                if (site.domains && site.domains.length) {
                    siteRows.push([prefix + '域名', site.domains.join('、')]);
                }
                siteRows.push([prefix + '网站地址', site.site_url, true]);
                siteRows.push([prefix + '后台地址', site.admin_url, true]);
                siteRows.push([prefix + '管理员账号', site.admin_user]);
                siteRows.push([prefix + '管理员密码', site.admin_password]);
                if (site.password_auto_generated) hasAutoPassword = true;
                if (!sslWarning && site.ssl && site.ssl.success === false) {
                    sslWarning = site.ssl.warning || SSL_FAILURE_WARNING;
                }
            });
        } else {
            siteRows = [
                ['网站名称', result.site_name],
                ['网站地址', result.site_url, true],
                ['后台地址', result.admin_url, true],
                ['管理员账号', result.admin_user],
                ['管理员密码', result.admin_password],
            ];
            hasAutoPassword = result.password_auto_generated;
            if (!sslWarning && result.ssl && result.ssl.success === false) {
                sslWarning = result.ssl.warning || SSL_FAILURE_WARNING;
            }
        }

        $('#result-table-baota').html(buildResultTableRows(baotaRows));
        $('#result-table-site').html(buildResultTableRows(siteRows));

        if (hasAutoPassword) {
            $('#result-password-notice').removeClass('hidden').html('部分网站管理员密码由系统自动生成，请妥善保存，登录后台后请<strong>尽快修改</strong>。');
        } else {
            $('#result-password-notice').addClass('hidden').empty();
        }

        if (sslWarning) {
            $('#result-ssl-notice').removeClass('hidden').text(sslWarning);
        } else {
            $('#result-ssl-notice').addClass('hidden').empty();
        }

        var firstSite = (result.sites && result.sites[0]) || result;
        $('#link-site').attr('href', firstSite.site_url || result.site_url || '#');
        $('#link-admin').attr('href', firstSite.admin_url || result.admin_url || '#');
        $('#deploy-panel').addClass('deploy-panel--success');
        $('#result-success').removeClass('hidden');
        $('#progress-title').text('部署完成');
        setPageGuard('success');
    }

    function finishSavedAndClose() {
        var token = currentToken || loadDeployToken();
        if (cancelRequestInFlight) return;

        setPageGuard(null);
        var $btn = $('#btn-saved-close').prop('disabled', true).text('正在关闭...');

        function done() {
            resetToNewDeploy();
            backToForm();
            toast('已关闭，相关信息已从本机清除', 'success');
            $btn.prop('disabled', false).text('已保存，关闭页面');
        }

        if (!token) {
            done();
            return;
        }

        cancelRequestInFlight = true;
        pauseDeployPolling();

        requestTaskCancel(token)
            .done(function (res) {
                if (!res || res.ok !== true) {
                    toast('删除任务记录失败，请稍后重试', 'error');
                    $btn.prop('disabled', false).text('已保存，关闭页面');
                    return;
                }
                done();
            })
            .fail(function () {
                toast('删除任务记录失败，请稍后重试', 'error');
                $btn.prop('disabled', false).text('已保存，关闭页面');
            })
            .always(function () {
                cancelRequestInFlight = false;
            });
    }

    function startDeployFlow() {
        if (!validateForm()) return;

        var $btn = $('#btn-start').prop('disabled', true).text('正在检测...');
        runPreflight()
            .done(function (res) {
                if (!res) return;
                renderPreflightResult(res);
                handlePreflightForDeploy(res);
            })
            .fail(function (xhr, status) {
                if (status === 'timeout') {
                    toast('环境检测超时，请检查 SSH 地址与网络', 'error');
                } else {
                    toast(parseAjaxErrorMessage(xhr, 'SSH 连接失败'), 'error');
                }
            })
            .always(function () {
                $btn.prop('disabled', false).text('一键开始搭建');
            });
    }

    $('#btn-preflight').on('click', function () {
        var $btn = $(this).prop('disabled', true).text('检测中...');
        runPreflight()
            .done(function (res) {
                renderPreflightResult(res);
                if (!res) return;
                if (!res.ssh_ok) {
                    toast(res.message || 'SSH 连接失败', 'error');
                } else if (res.blocked || res.domain_conflict) {
                    showEnvBlockedModal(res);
                } else if (res.ok) {
                    toast(res.is_fresh ? '环境检测通过：全新服务器' : '检测到非全新环境，继续搭建时将提示确认', 'success');
                } else {
                    toast(res.message || '检测失败', 'error');
                }
            })
            .fail(function (xhr) {
                toast(parseAjaxErrorMessage(xhr, 'SSH 连接失败'), 'error');
            })
            .always(function () {
                $btn.prop('disabled', false).text('检测服务器环境');
            });
    });

    $('[data-modal-close]').on('click', function () {
        var target = $(this).data('modal-close');
        if (target === 'env-risk') {
            $('#env-risk-modal').addClass('hidden');
            $('#env-warning-modal').removeClass('hidden');
            return;
        }
        if (target === 'env-warning' || target === 'env-blocked') {
            hideEnvModals();
        }
    });

    $('#btn-env-continue').on('click', function () {
        if (!pendingEnvPreflight) return;
        showEnvModal('env-risk-modal');
    });

    $('#btn-env-risk-confirm').on('click', function () {
        if (!pendingEnvPreflight) return;
        var res = pendingEnvPreflight;
        hideEnvModals();
        enterConfirmPage(res);
    });

    $('#deploy-form').on('submit', function (e) {
        e.preventDefault();
        startDeployFlow();
    });

    $('#btn-confirm-start').on('click', function () {
        submitDeploy();
    });

    $('#btn-back-edit').on('click', function () {
        backToForm();
    });

    $('#btn-copy-link').on('click', function () {
        var text = $('#task-link').text();
        if (navigator.clipboard) {
            navigator.clipboard.writeText(text).then(function () {
                toast('已复制', 'success');
            });
        } else {
            toast(text);
        }
    });

    $('#btn-retry').on('click', function () {
        retryDeploy();
    });

    $('#btn-saved-close').on('click', function () {
        finishSavedAndClose();
    });

    $(window).on('beforeunload', function (e) {
        if (!pageGuardMode) return;
        var message = pageGuardMode === 'running' ? '当前任务正在进行中，请暂时不要关闭页面，避免任务失败。' : '请确认已保存并记录好您的网站信息。关闭后，此信息将无法再次查看。';
        e.preventDefault();
        e.returnValue = message;
        return message;
    });

    $('#btn-cancel-deploy, #btn-cancel-failed').on('click', function () {
        cancelDeploy();
    });

    function resumeDeployFromStorage() {
        var token = loadDeployToken();
        if (!token) return;

        $('#form-section, #page-alert').addClass('hidden');
        $('#progress-section').removeClass('hidden');
        showDeployPanel();
        showProgressLayout(token);

        pollDeployOnce(token, function (res) {
            if (!res || res.expired) {
                clearDeployToken();
                return;
            }
            if (!res.done) {
                setPageGuard('running');
                startDeployPolling(token);
            }
        });
    }

    $form().on('input change', '[name="ssh_host"], [name="ssh_password"], [name="ssh_port"], [name="ssh_user"], [name="server_os"]', function () {
        lastPreflight = null;
        $('#preflight-result').addClass('hidden').empty();
    });

    $('#btn-add-site').on('click', function () {
        addSiteCard();
    });

    $('#sites-container').on('click', '.btn-remove-site', function () {
        if ($('#sites-container .site-card').length <= 1) return;
        $(this).closest('.site-card').remove();
        refreshSiteCardIndexes();
    });

    addSiteCard();
    resumeDeployFromStorage();
})(jQuery);
