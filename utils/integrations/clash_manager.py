import os
import yaml
import docker
import requests

BASE_PATH = os.path.join(os.getcwd(), "data", "mihomo-pool")
os.makedirs(BASE_PATH, exist_ok=True)

HOST_PROJECT_PATH = os.getenv('HOST_PROJECT_PATH', os.getcwd())
HOST_BASE_PATH = os.path.join(HOST_PROJECT_PATH, "data", "mihomo-pool")

IMAGE_NAME = "metacubex/mihomo:latest"


def get_client():
    try:
        return docker.from_env()
    except Exception as e:
        print(f"[!] Docker 连接失败: {e}")
        return None


def get_pool_status():
    client = get_client()
    if not client: return {"instances": [], "groups": [], "error": "Docker 套接字未挂载"}

    instances = []
    containers = client.containers.list(all=True, filters={"name": "clash_"})
    for c in containers:
        p_map = c.attrs.get('HostConfig', {}).get('PortBindings', {})
        ports = [f"{b[0]['HostPort']}->{p.split('/')[0]}" for p, b in p_map.items() if b]
        instances.append({
            "name": c.name, "status": c.status, "ports": ", ".join(ports)
        })
    instances.sort(key=lambda x: int(x['name'].split('_')[1]) if '_' in x['name'] else 999)

    groups = []
    sample_cfg = os.path.join(BASE_PATH, "clash_1", "config.yaml")
    if os.path.exists(sample_cfg):
        try:
            with open(sample_cfg, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
                for g in data.get('proxy-groups', []):
                    groups.append({
                        "name": g.get('name', 'N/A'),
                        "count": len(g.get('proxies', [])),
                        "type": g.get('type', 'N/A')
                    })
        except:
            pass

    return {"instances": instances, "groups": groups}


def deploy_clash_pool(count):
    client = get_client()
    if not client: return False, "Docker未就绪"

    for c in client.containers.list(all=True, filters={"name": "clash_"}):
        try:
            if int(c.name.split('_')[1]) > count: c.remove(force=True)
        except:
            pass

    for i in range(1, count + 1):
        name = f"clash_{i}"
        inst_dir = os.path.join(BASE_PATH, name)
        os.makedirs(inst_dir, exist_ok=True)
        cfg_file = os.path.join(inst_dir, "config.yaml")

        if not os.path.exists(cfg_file):
            with open(cfg_file, 'w') as f: f.write("allow-lan: true\nmixed-port: 7890")

        try:
            client.containers.get(name)
        except docker.errors.NotFound:
            client.containers.run(
                IMAGE_NAME, name=name, detach=True, restart_policy={"Name": "always"},
                ports={'7890/tcp': 41000 + i, '9090/tcp': 42000 + i},
                volumes={os.path.join(HOST_BASE_PATH, name, "config.yaml"): {'bind': '/root/.config/mihomo/config.yaml',
                                                                             'mode': 'rw'}}
            )
    return True, f"成功同步 {count} 个实例"


def patch_and_update(url, target):
    client = get_client()
    try:
        headers = {
            "User-Agent": "Clash-meta",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        }
        r = requests.get(url, headers=headers, timeout=30)
        r.raise_for_status()
        raw_yaml = yaml.safe_load(r.text)

        conts = client.containers.list(all=True, filters={"name": "clash_"})
        indices = range(1, len(conts) + 1) if target == 'all' else [int(target)]

        for i in indices:
            name = f"clash_{i}"
            patched = raw_yaml.copy()
            patched.update({'mixed-port': 7890, 'allow-lan': True, 'external-controller': '0.0.0.0:9090'})
            with open(os.path.join(BASE_PATH, name, "config.yaml"), 'w', encoding='utf-8') as f:
                yaml.dump(patched, f, allow_unicode=True)

            try:
                client.containers.get(name).restart()
            except:
                pass

        return True, "订阅已更新并应用补丁"
    except Exception as e:
        return False, str(e)