export const defaultSkillServer = 'http://10.37.24.3:8766';

export function buildSkillInstallPrompt(server = defaultSkillServer) {
  const base = server.replace(/\/$/, '');
  const endpoint = `${base}/api/skills/archive`;
  const manifest = `${base}/api/skills/manifest`;
  const page = `${base}/skills.html`;
  return `请为我安装 Trace Hunter 客户端 Skills。版本页是 ${page}，机器检查接口是 ${manifest}。请通过 ${endpoint} 下载官方压缩包，先读取 trace-hunter-client/manifest.json，再将 trace-hunter-client/skills 下的每个 Skill 安装到当前运行环境支持的 Skill 目录。后续使用前直接访问机器检查接口，并与官方压缩包逐文件比较。trace-hunter-cli 已自带可执行 CLI 和线上服务配置，不要另外查找仓库，也不要为 authentication=none 的服务索要账号密码。安装前检查同名 Skill；若存在，比较内容并保留用户修改，不要直接覆盖。安装后重新扫描或重启 Skill 加载机制，并按 manifest 中的 URL、项目执行 capabilities，验证线上接口可访问。最后报告安装路径、Skill 列表、CLI 路径、服务地址、冲突处理和验证结果。`;
}
