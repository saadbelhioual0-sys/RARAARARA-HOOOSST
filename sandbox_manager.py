import docker
import random
import asyncio
import os
from docker.errors import NotFound, APIError

docker_client = docker.from_env()

async def create_user_container(server_id: int, port: int, user_code_path: str):
    """إنشاء حاوية Docker معزولة لكل سيرفر"""
    container_name = f"jagwar_server_{server_id}"
    
    try:
        container = docker_client.containers.run(
            image="python:3.11-slim",
            command="sleep infinity",
            name=container_name,
            detach=True,
            ports={f"8000/tcp": port},
            environment={
                "PORT": str(port),
                "SERVER_ID": str(server_id)
            },
            volumes={
                os.path.abspath(user_code_path): {
                    "bind": "/app",
                    "mode": "rw"
                }
            },
            mem_limit="512m",
            cpu_quota=50000,  # 0.5 CPU core
            network_mode="bridge",
            restart_policy={"Name": "no"}
        )
        return container.id
    except APIError as e:
        print(f"Docker error: {e}")
        return None

async def start_python_server(container_id: str, server_dir: str):
    """تشغيل python main.py داخل الحاوية مع تثبيت المكتبات أولاً"""
    container = docker_client.containers.get(container_id)
    
    # تثبيت المكتبات من requirements.txt لو موجود
    install_cmd = "cd /app && if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi"
    container.exec_run(install_cmd, workdir="/app")
    
    # تشغيل السيرفر
    exec_instance = container.exec_run(
        "cd /app && python main.py",
        workdir="/app",
        detach=True,
        stream=True
    )
    return exec_instance

async def stop_container(container_id: str):
    try:
        container = docker_client.containers.get(container_id)
        container.stop()
        container.remove()
        return True
    except NotFound:
        return False

async def get_container_stats(container_id: str):
    try:
        container = docker_client.containers.get(container_id)
        stats = container.stats(stream=False)
        cpu_usage = stats["cpu_stats"]["cpu_usage"]["total_usage"]
        system_cpu = stats["cpu_stats"]["system_cpu_usage"]
        cpu_percent = round((cpu_usage / system_cpu) * 100, 2) if system_cpu else 0
        mem_usage = stats["memory_stats"]["usage"]
        mem_limit = stats["memory_stats"]["limit"]
        mem_percent = round((mem_usage / mem_limit) * 100, 2) if mem_limit else 0
        return {
            "cpu": f"{cpu_percent}%",
            "ram": f"{round(mem_usage / 1024 / 1024, 2)}MB",
            "ram_percent": f"{mem_percent}%"
        }
    except:
        return {"cpu": "0%", "ram": "0MB", "ram_percent": "0%"}

async def execute_command_in_container(container_id: str, command: str):
    """تنفيذ أي أمر داخل الحاوية (للكونسول)"""
    try:
        container = docker_client.containers.get(container_id)
        exec_result = container.exec_run(command, workdir="/app", stream=True)
        output = ""
        for line in exec_result.output:
            output += line.decode()
        return output
    except Exception as e:
        return f"Error: {str(e)}"
