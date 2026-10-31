ARG PHP_VERSION=8.5
FROM php:${PHP_VERSION}-fpm-alpine

# PECL extensions are built from their release tarballs because pecl itself
# is no longer shipped from PHP 8.6 on.
ARG IMAGICK_VERSION
ARG REDIS_VERSION

LABEL maintainer="MegaThorx <megathorx@merx.dev>"

RUN set -eux; \
    test -n "$IMAGICK_VERSION"; \
    test -n "$REDIS_VERSION"; \
    apk upgrade --no-cache; \
    apk add --no-cache \
        nginx \
        supervisor \
        bash \
        curl \
        ca-certificates \
        imagemagick \
        imagemagick-jpeg \
        imagemagick-webp; \
    apk add --no-cache --virtual .build-deps \
        $PHPIZE_DEPS \
        linux-headers \
        freetype-dev \
        libjpeg-turbo-dev \
        libpng-dev \
        libwebp-dev \
        gmp-dev \
        icu-dev \
        libzip-dev \
        libxslt-dev \
        libxml2-dev \
        sqlite-dev \
        imagemagick-dev; \
    docker-php-source extract; \
    for ext in "imagick-$IMAGICK_VERSION" "redis-$REDIS_VERSION"; do \
        dir="/usr/src/php/ext/${ext%-*}"; \
        mkdir -p "$dir"; \
        curl -fsSL "https://pecl.php.net/get/$ext.tgz" | tar -xz --strip-components=1 -C "$dir"; \
    done; \
    docker-php-ext-configure gd --with-freetype --with-webp --with-jpeg; \
    docker-php-ext-install -j"$(nproc)" pdo_mysql mysqli gd exif gmp intl xsl soap zip imagick redis; \
    # OPcache is compiled in from PHP 8.5 on
    php -m | grep -qix 'zend opcache' || docker-php-ext-install -j"$(nproc)" opcache; \
    runDeps="$( \
        scanelf --needed --nobanner --format '%n#p' --recursive /usr/local/lib/php/extensions \
            | tr ',' '\n' \
            | sort -u \
            | awk 'system("[ -e /usr/local/lib/" $1 " ]") == 0 { next } { print "so:" $1 }' \
    )"; \
    apk add --no-cache --virtual .run-deps $runDeps; \
    apk del --no-network .build-deps; \
    docker-php-source delete; \
    rm -rf /tmp/*; \
    mkdir -p /run/nginx

COPY conf/nginx.conf /etc/nginx/nginx.conf
COPY conf/nginx-site.conf /etc/nginx/sites-available/default.conf

RUN mkdir -p /etc/nginx/sites-enabled && \
    ln -s /etc/nginx/sites-available/default.conf /etc/nginx/sites-enabled/default.conf

RUN echo "cgi.fix_pathinfo=0" > /usr/local/etc/php/conf.d/docker-vars.ini &&\
    echo "upload_max_filesize = 100M"  >> /usr/local/etc/php/conf.d/docker-vars.ini&&\
    echo "post_max_size = 100M"  >> /usr/local/etc/php/conf.d/docker-vars.ini &&\
    echo "variables_order = \"EGPCS\""  >> /usr/local/etc/php/conf.d/docker-vars.ini && \
    echo "memory_limit = 512M"  >> /usr/local/etc/php/conf.d/docker-vars.ini && \
    sed -i \
        -e "s/;catch_workers_output\s*=\s*yes/catch_workers_output = yes/g" \
        -e "s/pm.max_children = 5/pm.max_children = 4/g" \
        -e "s/pm.start_servers = 2/pm.start_servers = 3/g" \
        -e "s/pm.min_spare_servers = 1/pm.min_spare_servers = 2/g" \
        -e "s/pm.max_spare_servers = 3/pm.max_spare_servers = 4/g" \
        -e "s/;pm.max_requests = 500/pm.max_requests = 200/g" \
        -e "s/user = www-data/user = nginx/g" \
        -e "s/group = www-data/group = nginx/g" \
        -e "s/;listen.mode = 0660/listen.mode = 0666/g" \
        -e "s/;listen.owner = www-data/listen.owner = nginx/g" \
        -e "s/;listen.group = www-data/listen.group = nginx/g" \
        -e "s/^;*listen = 127.0.0.1:9000/listen = \/var\/run\/php-fpm.sock/g" \
        -e "s/^;clear_env = no$/clear_env = no/" \
        /usr/local/etc/php-fpm.d/www.conf && \
    # php-fpm only reads www.conf, so it must end up with an active listen line
    grep -qx 'listen = /var/run/php-fpm.sock' /usr/local/etc/php-fpm.d/www.conf

COPY conf/supervisord.conf /etc/supervisord.conf
COPY src/ /var/www/html/

EXPOSE 80

WORKDIR "/var/www/html"
CMD ["/usr/bin/supervisord", "-n", "-c", "/etc/supervisord.conf"]
