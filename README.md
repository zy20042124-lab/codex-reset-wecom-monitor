# Codex 重置预报：企业微信机器人版

这个小工具由 GitHub Actions 每小时读取一次
[`codex-reset.com/api/forecast`](https://codex-reset.com/api/forecast)，并把需要关注的变化推送到企业微信群。

> [!IMPORTANT]
> `codex-reset.com` 是第三方独立站点，不属于 OpenAI。这里监控的是公开的全局重置预测和重置记录，
> 不是你个人账号的 5 小时/每周额度，也不能保证未来一定发生重置。

## 会推送什么

- 未来 24 小时预测概率从 `80%` 或以下上升到严格大于 `80%`；
- 第三方接口将一条新的公开 X 消息标为明确的重置预告时，立即提醒（不受 24 小时概率阈值限制）；
- 第三方接口的 `last_reset_at` 出现更晚的时间；
- 接口连续 3 次读取失败，以及之后恢复；
- 手动触发的企业微信测试消息。

同一轮高概率、同一条公开预告或同一条重置记录只推送一次。公开预告消息会包含原帖摘要和链接；它是
第三方对公开信息的识别，不代表已实际到账。非敏感状态保存在自动创建的 `monitor-state` 分支，
Webhook 只保存在 GitHub Actions Secret 中。

机器人统一发送纯文本 `text` 消息，以便已绑定企业微信的个人微信也能直接显示正文，而不是提示前往企业微信查看。

所有展示时间统一为北京时间（UTC+8）。接口返回的 `Z` 时间代表 UTC，程序会按时区只转换一次；若公开消息里
包含可可靠解析的 PT/PST 预计窗口，程序使用第三方接口给出的标准化时间窗口再换算。消息会明确区分
“X 公告/确认时间”和“预计重置窗口”，没有可靠窗口时不自行猜测。

## 部署

1. 在 GitHub 新建一个仓库。建议使用公开仓库：标准 GitHub-hosted runner 对公开仓库免费。
2. 把本目录的文件提交并推送到仓库默认分支。
3. 打开仓库 **Settings → Secrets and variables → Actions**。
4. 新建 **Repository secret**：

   - Name：`WECOM_WEBHOOK`
   - Secret：企业微信群机器人完整 Webhook 地址

5. 打开 **Actions → Codex Reset WeCom Monitor → Run workflow**。
6. 选择 `send-test-notification` 后运行，确认企业微信群收到测试消息。
7. 再选择 `check-now` 运行一次，建立初始状态。首次只记录已有的重置时间，不会把旧记录当成新重置推送；如果当时预测已高于 80%，会发一次概率提醒。

定时任务按 UTC 每小时第 17 分钟触发，即大约每小时一次。GitHub 明确说明定时任务在高负载时可能延迟，
所以它适合预告和提醒，不适合秒级告警。

## 修改阈值

编辑 [`.github/workflows/monitor.yml`](.github/workflows/monitor.yml) 中的：

```yaml
ALERT_THRESHOLD: "80"
```

例如改成 `70`，表示从 70% 或以下上穿到严格大于 70% 时提醒。

## 本地离线测试

```powershell
python -m unittest discover -s tests -v
```

单元测试不会访问第三方接口，也不会发送企业微信消息。

## 安全说明

- 不要把 Webhook 写入源码、README、Issue、日志或截图；拿到 Webhook 的人可以向群里发消息。
- 工作流只监听 `schedule` 和手动触发，不监听来自 Pull Request 的代码。
- `GITHUB_TOKEN` 只申请 `contents: write`，用于维护 `monitor-state` 状态分支。
- 程序会隐藏企业微信 Webhook 地址，网络错误日志不会包含机器人密钥。
- 如果 Webhook 曾经泄露，请立即在企业微信群里删除并重新创建机器人。

## 已知限制

- GitHub Actions 的定时运行不是精确定时器，可能延迟或偶尔排队。
- 第三方接口或字段变化时，监控会报错并重试。
- 极少数情况下，消息发送成功但状态写回失败，下一轮可能重复推送一次；这样设计是为了避免漏报。
- 公开仓库连续 60 天无活动时，GitHub 可能自动停用定时工作流。程序每 30 天写一次非敏感心跳状态，
  但仍建议偶尔查看 Actions 页面是否正常运行。
