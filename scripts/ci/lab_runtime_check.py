"""Run a built Lab image with disposable dependencies; never use a live database."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import time
import urllib.request
import uuid

PORTS = dict(zip(('order-service', 'inventory-service', 'user-service', 'payment-service',
                 'notification-service', 'recommendation-service'), range(8080, 8086)))


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True).strip()


def check(service, image, sha):
    if service not in PORTS or not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise ValueError('known service and full source SHA required')
    info = json.loads(docker('image', 'inspect', image))[0]
    if info['Config'].get('Labels', {}).get('org.opencontainers.image.revision') != sha:
        raise ValueError('OCI revision differs from source SHA')
    prefix = 'sre-ci-' + uuid.uuid4().hex[:12]
    names = []
    docker('network', 'create', prefix)
    try:
        env = {'SERVICE_VERSION': sha, 'POD_NAME': prefix, 'SKYWALKING_AGENT_ENABLED': 'false',
               'OTEL_SDK_DISABLED': 'true'}
        if service in {'order-service', 'user-service'}:
            db = prefix + '-mysql'
            names.append(db)
            schema = Path(__file__).resolve().parents[2] / 'sre-broken-system/sre-lab-infra/mysql/init/001-schema.sql'
            docker('run', '-d', '--name', db, '--network', prefix, '--network-alias', 'mysql',
                   '-e', 'MYSQL_ROOT_PASSWORD=ci-disposable-only',
                   '--mount', f'type=bind,src={schema},dst=/docker-entrypoint-initdb.d/schema.sql,readonly', 'mysql:8.4')
            for _ in range(120):
                result = subprocess.run(['docker', 'exec', db, 'mysql', '-h127.0.0.1', '-usre_app',
                                         '-psre_app_dev_only', '-Dsre_lab', '-e', 'SELECT 1'], capture_output=True)
                if result.returncode == 0:
                    break
                time.sleep(1)
            else:
                raise RuntimeError('disposable MySQL initialization timed out')
            env.update(DB_URL='jdbc:mysql://mysql:3306/sre_lab?useSSL=false&allowPublicKeyRetrieval=true',
                       DB_USERNAME='sre_app', DB_PASSWORD='sre_app_dev_only',
                       DATABASE_URL='mysql+pymysql://sre_app:sre_app_dev_only@mysql:3306/sre_lab')
        names.append(prefix)
        args = ['run', '-d', '--name', prefix, '--network', prefix, '-p', f'127.0.0.1::{PORTS[service]}']
        for key, value in env.items():
            args.extend(['-e', f'{key}={value}'])
        docker(*args, image)
        address = docker('port', prefix, f'{PORTS[service]}/tcp').splitlines()[0]
        health = '/actuator/health/readiness' if service == 'order-service' else '/health'
        for _ in range(120):
            try:
                with urllib.request.urlopen('http://' + address + health, timeout=3) as response:
                    if response.status == 200:
                        break
            except (OSError, TimeoutError):
                pass
            time.sleep(1)
        else:
            raise RuntimeError('image health check timed out')
        for path in ['/actuator/prometheus' if service == 'order-service' else '/metrics', '/debug/fault']:
            with urllib.request.urlopen('http://' + address + path, timeout=10) as response:
                body = response.read()
                if response.status != 200 or not body:
                    raise RuntimeError(f'empty/failed runtime endpoint: {path}')
        print(f'{service}: OCI revision, health, metrics and fault API passed')
    except Exception:
        for name in names:
            subprocess.run(['docker', 'logs', '--tail', '80', name], check=False)
        raise
    finally:
        for name in reversed(names):
            subprocess.run(['docker', 'rm', '-fv', name], check=True)
        docker('network', 'rm', prefix)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--service', choices=list(PORTS), required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--sha', required=True)
    args = parser.parse_args()
    check(args.service, args.image, args.sha)
