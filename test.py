import subprocess

from mcp.server.fastmcp import FastMCP

# 创建 FastMCP 实例
mcp = FastMCP("OpenCLI-Service")


@mcp.tool()
def execute_opencli(command: str, args: list[str] = None) -> str:
    """
    调用本地 opencli 执行特定任务。
    :param command: opencli 的子命令 (例如 'login', 'status')
    :param args: 附加参数列表
    """
    full_cmd = ["opencli", command]
    if args:
        full_cmd.extend(args)

    try:
        # 执行本地命令并捕获输出
        result = subprocess.run(full_cmd, capture_output=True, text=True, check=True)
        return f"Output:\n{result.stdout}"
    except subprocess.CalledProcessError as e:
        return f"Error executing opencli: {e.stderr}"


if __name__ == "__main__":
    mcp.run()
