#!/bin/bash
# 由 server_optimize.py 动态生成并执行；此文件为参考模板。
# 根据服务器内存/CPU 自动设置 PHP、PHP-FPM、MySQL 参数，并安装 Redis 扩展、启用 Opcache。

PHP_SHORT="{{ php_short }}"
PHP_ROOT="/www/server/php/${PHP_SHORT}"
PHP_INI="${PHP_ROOT}/etc/php.ini"
FPM_POOL="${PHP_ROOT}/etc/php-fpm.d/www.conf"

# PHP ini
# memory_limit, upload_max_filesize, post_max_size, max_execution_time, max_input_vars
# opcache.enable=1

# PHP-FPM www.conf
# pm.max_children, pm.start_servers, pm.min_spare_servers, pm.max_spare_servers

# MySQL /etc/my.cnf.d/qbw-optimize.cnf
# innodb_buffer_pool_size, max_connections
