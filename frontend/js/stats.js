(function ($) {
  'use strict';

  var ROOT = window.QBW_ROOT || '#qbw-stats-app';
  var API_BASE = window.QBW_API_BASE || 'http://localhost:8000';
  var STATS_TOKEN = window.QBW_STATS_TOKEN || '';
  var STORAGE_API_KEY = 'qbw_stats_api_base';
  var STORAGE_TOKEN_KEY = 'qbw_stats_token';
  var PAGE_SIZE = 20;

  var state = {
    page: 1,
    pages: 1,
    total: 0,
    statusFilter: '',
    sortBy: 'created_at',
    sortOrder: 'desc',
    loading: false
  };

  function $root() {
    return $(ROOT);
  }

  function esc(text) {
    return $('<div/>').text(text == null ? '' : String(text)).html();
  }

  function formatDate(value) {
    if (!value) return '-';
    var date = new Date(value);
    if (isNaN(date.getTime())) return esc(value);
    return esc(date.toLocaleString('zh-CN', { hour12: false }));
  }

  function formatFinishedAt(value, status) {
    if (status === 'running' || !value) {
      return '<span class="unfinished-label">' + esc('未完成') + '</span>';
    }
    return formatDate(value);
  }

  function sortIndicator(sortBy) {
    if (state.sortBy !== sortBy) {
      return '';
    }
    return state.sortOrder === 'asc' ? '↑' : '↓';
  }

  function updateSortHeaders() {
    $root().find('.sortable-th').each(function () {
      var $btn = $(this);
      var sortBy = $btn.data('sort');
      var isActive = sortBy === state.sortBy;
      $btn.toggleClass('is-active', isActive);
      $btn.find('.sort-indicator').text(sortIndicator(sortBy));
    });
  }

  function statusLabel(status) {
    if (status === 'running') return '正在部署中';
    if (status === 'success') return '成功';
    if (status === 'failed') return '失败';
    if (status === 'cancelled') return '已取消';
    return status || '-';
  }

  function statusClass(status) {
    if (status === 'running') return 'status-running';
    if (status === 'success') return 'status-success';
    if (status === 'failed') return 'status-failed';
    if (status === 'cancelled') return 'status-cancelled';
    return '';
  }

  function phaseLabel(phase) {
    if (!phase) return '-';
    return esc(phase.replace(/^step\d+_/, '').replace(/_/g, ' '));
  }

  function renderSites(sites) {
    if (!sites || !sites.length) {
      return '<span>-</span>';
    }
    var html = '<ul class="site-list">';
    sites.forEach(function (site) {
      var name = site.site_name || site.primary_domain || '未命名';
      var domain = site.primary_domain || (site.domains && site.domains[0]) || '';
      html += '<li><strong>' + esc(name) + '</strong>';
      if (domain) {
        html += ' · ' + esc(domain);
      }
      if (site.domains && site.domains.length > 1) {
        html += '<br><span class="hint">' + esc(site.domains.join(', ')) + '</span>';
      }
      html += '</li>';
    });
    html += '</ul>';
    return html;
  }

  function getApiBase() {
    if (window.QBW_API_BASE) {
      return String(window.QBW_API_BASE).replace(/\/$/, '');
    }
    var input = $root().find('#stats_api_base').val();
    return String(input || API_BASE).replace(/\/$/, '');
  }

  function getToken() {
    if (window.QBW_STATS_TOKEN) {
      return String(window.QBW_STATS_TOKEN);
    }
    var input = $root().find('#stats_token').val();
    return String(input || STATS_TOKEN || '');
  }

  function saveSessionConfig() {
    if (window.QBW_STATS_TOKEN) {
      return;
    }
    sessionStorage.setItem(STORAGE_API_KEY, getApiBase());
    sessionStorage.setItem(STORAGE_TOKEN_KEY, getToken());
  }

  function restoreSessionConfig() {
    if (window.QBW_STATS_TOKEN) {
      return;
    }
    var savedApi = sessionStorage.getItem(STORAGE_API_KEY);
    var savedToken = sessionStorage.getItem(STORAGE_TOKEN_KEY);
    if (savedApi) {
      $root().find('#stats_api_base').val(savedApi);
    } else {
      $root().find('#stats_api_base').val(API_BASE);
    }
    if (savedToken) {
      $root().find('#stats_token').val(savedToken);
    }
  }

  function showError(message) {
    var $error = $root().find('#stats-error');
    if (!message) {
      $error.addClass('hidden').text('');
      return;
    }
    $error.removeClass('hidden').text(message);
  }

  function authHeaders() {
    return {
      Authorization: 'Bearer ' + getToken()
    };
  }

  function apiRequest(path, params) {
    return $.ajax({
      url: getApiBase() + path,
      method: 'GET',
      headers: authHeaders(),
      data: params || {},
      timeout: 30000
    });
  }

  function updateSummaryMetric(key, value) {
    $root().find('[data-key="' + key + '"]').text(value);
  }

  function renderSummary(data) {
    ['today', 'week', 'month', 'all_time'].forEach(function (period) {
      var block = data[period] || {};
      updateSummaryMetric(period + '-total', block.total || 0);
      updateSummaryMetric(period + '-success', block.success || 0);
      updateSummaryMetric(period + '-failed', block.failed || 0);
    });
  }

  function renderTable(items) {
    var $body = $root().find('#stats-table-body');
    $body.empty();

    if (!items || !items.length) {
      $body.append('<tr><td colspan="7" class="empty-cell">暂无数据</td></tr>');
      return;
    }

    items.forEach(function (item) {
      var row = '<tr>'
        + '<td>' + formatDate(item.created_at) + '</td>'
        + '<td>' + formatFinishedAt(item.finished_at, item.status) + '</td>'
        + '<td>' + esc(item.client_ip || '-') + '</td>'
        + '<td>' + renderSites(item.sites) + '</td>'
        + '<td><span class="status-badge ' + statusClass(item.status) + '">' + esc(statusLabel(item.status)) + '</span></td>'
        + '<td>' + phaseLabel(item.failed_phase) + '</td>'
        + '<td class="error-cell">' + esc(item.error_summary || '-') + '</td>'
        + '</tr>';
      $body.append(row);
    });
  }

  function updatePagination() {
    $root().find('#stats-page-info').text('第 ' + state.page + ' / ' + state.pages + ' 页（共 ' + state.total + ' 条）');
    $root().find('#btn-stats-prev').prop('disabled', state.page <= 1 || state.loading);
    $root().find('#btn-stats-next').prop('disabled', state.page >= state.pages || state.loading);
  }

  function loadSummary() {
    return apiRequest('/api/admin/stats/summary').then(function (data) {
      renderSummary(data);
    });
  }

  function loadList() {
    var params = {
      page: state.page,
      page_size: PAGE_SIZE,
      sort_by: state.sortBy,
      sort_order: state.sortOrder
    };
    if (state.statusFilter) {
      params.status = state.statusFilter;
    }
    return apiRequest('/api/admin/stats', params).then(function (data) {
      state.total = data.total || 0;
      state.pages = data.pages || 1;
      if (state.page > state.pages) {
        state.page = state.pages;
      }
      renderTable(data.items || []);
      updateSortHeaders();
      updatePagination();
    });
  }

  function loadAll() {
    if (state.loading) {
      return $.Deferred().reject();
    }
    var token = getToken();
    if (!token) {
      showError('请先填写管理 Token');
      return $.Deferred().reject();
    }

    state.loading = true;
    showError('');
    saveSessionConfig();
    $root().find('#stats-content').removeClass('hidden');

    return $.when(loadSummary(), loadList())
      .fail(function (xhr) {
        var message = '加载失败';
        if (xhr && xhr.responseJSON && xhr.responseJSON.detail) {
          message = xhr.responseJSON.detail;
        } else if (xhr && xhr.status === 401) {
          message = 'Token 无效，请检查后重试';
        } else if (xhr && xhr.status === 503) {
          message = '后端未配置 ADMIN_STATS_TOKEN';
        }
        showError(message);
      })
      .always(function () {
        state.loading = false;
        updatePagination();
      });
  }

  function bindEvents() {
    $root().on('click', '#btn-stats-connect', function () {
      state.page = 1;
      loadAll();
    });

    $root().on('click', '#btn-stats-prev', function () {
      if (state.page <= 1 || state.loading) return;
      state.page -= 1;
      loadList();
    });

    $root().on('click', '#btn-stats-next', function () {
      if (state.page >= state.pages || state.loading) return;
      state.page += 1;
      loadList();
    });

    $root().on('change', '#stats-status-filter', function () {
      state.statusFilter = $(this).val() || '';
      state.page = 1;
      if ($root().find('#stats-content').is(':visible')) {
        loadAll();
      }
    });

    $root().on('click', '.sortable-th', function () {
      if (state.loading) return;
      var sortBy = $(this).data('sort');
      if (!sortBy) return;
      if (state.sortBy === sortBy) {
        state.sortOrder = state.sortOrder === 'asc' ? 'desc' : 'asc';
      } else {
        state.sortBy = sortBy;
        state.sortOrder = sortBy === 'client_ip' ? 'asc' : 'desc';
      }
      state.page = 1;
      if ($root().find('#stats-content').is(':visible')) {
        loadList();
      } else {
        updateSortHeaders();
      }
    });
  }

  function initStandaloneConfig() {
    if (window.QBW_STATS_TOKEN) {
      $root().find('#stats-config-section').addClass('hidden');
      loadAll();
      return;
    }
    restoreSessionConfig();
  }

  $(function () {
    if (!$root().length) {
      return;
    }
    bindEvents();
    updateSortHeaders();
    initStandaloneConfig();
  });
}(jQuery));
