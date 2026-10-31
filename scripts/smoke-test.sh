#!/usr/bin/env bash
# Usage: smoke-test.sh <image> <php-cycle>
#
# Starts the image and checks that nginx serves PHP through php-fpm, that all
# expected extensions are loaded and that Imagick can encode images. Prints the
# component versions and appends them to $GITHUB_OUTPUT when it is set.
set -euo pipefail

image=$1
cycle=$2
port=${SMOKE_TEST_PORT:-8080}
name="smoke-test-$$"

extensions=(dom exif gd gmp imagick intl mysqli pdo_mysql pdo_sqlite redis soap xsl zip "Zend OPcache")

fail() {
    echo "FAIL: $*" >&2
    docker logs "$name" >&2 || true
    exit 1
}

cleanup() {
    docker rm -f "$name" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker run -d --name "$name" -p "127.0.0.1:$port:80" "$image" >/dev/null

body=
for _ in $(seq 30); do
    if body=$(curl -fsS "http://127.0.0.1:$port/" 2>/dev/null); then
        break
    fi
    sleep 1
done
grep -q "PHP Version $cycle\." <<<"$body" || fail "phpinfo page for PHP $cycle not served"

modules=$(docker exec "$name" php -m)
for ext in "${extensions[@]}"; do
    grep -qixF "$ext" <<<"$modules" || fail "extension $ext not loaded"
done

docker exec "$name" nginx -t >/dev/null 2>&1 || fail "nginx -t failed"

[ "$(docker exec "$name" php -r 'echo ini_get("memory_limit");')" = 512M ] || fail "docker-vars.ini not applied"

docker exec "$name" php -r '
    $image = new Imagick();
    $image->newImage(1, 1, "white");
    foreach (["png", "jpeg", "webp", "gif"] as $format) {
        $image->setImageFormat($format);
        if ($image->getImageBlob() === "") {
            fwrite(STDERR, "cannot encode $format\n");
            exit(1);
        }
    }
' || fail "Imagick cannot encode images"

php_version=$(docker exec "$name" php -r 'echo PHP_VERSION;')
nginx_version=$(docker exec "$name" nginx -v 2>&1 | sed 's|.*nginx/||')
alpine_version=$(docker exec "$name" cat /etc/alpine-release)

echo "OK: $image (PHP $php_version, nginx $nginx_version, Alpine $alpine_version)"
if [ -n "${GITHUB_OUTPUT:-}" ]; then
    {
        echo "php_version=$php_version"
        echo "nginx_version=$nginx_version"
        echo "alpine_version=$alpine_version"
    } >>"$GITHUB_OUTPUT"
fi
