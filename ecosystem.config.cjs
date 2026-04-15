// PM2 ecosystem config — 放在项目根目录
// 启动：pm2 start ecosystem.config.cjs
// 重启：pm2 restart thccb-backend
// 日志：pm2 logs thccb-backend

module.exports = {
  apps: [
    {
      name: 'thccb-backend',
      cwd: './backend',
      script: './venv/bin/uvicorn',
      args: 'app.main:app --host 127.0.0.1 --port 8004 --workers 2',
      interpreter: 'none',  // 不用 node 解释，直接运行 uvicorn 二进制
      env: {
        // 从 backend/.env 读取，这里只放运行时覆盖
        PYTHONPATH: '.',
      },
      // 进程管理
      instances: 1,           // uvicorn 自己管 workers，pm2 只管主进程
      autorestart: true,
      max_restarts: 10,
      restart_delay: 3000,    // 崩溃后 3 秒再重启
      max_memory_restart: '512M',

      // 优雅停机：给 uvicorn 时间排空连接和 SSE 流
      kill_timeout: 8000,     // 发 SIGTERM 后等 8 秒再 SIGKILL
      listen_timeout: 5000,   // 等待新进程 ready 的超时
      shutdown_with_message: false,

      // 日志
      log_date_format: 'YYYY-MM-DD HH:mm:ss',
      error_file: './logs/backend-error.log',
      out_file: './logs/backend-out.log',
      merge_logs: true,

      // 文件监听（开发用，生产建议关掉）
      watch: false,
    },
  ],
}
