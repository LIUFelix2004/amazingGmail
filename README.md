# amazingGmail — Google Workspace 批量账号管理

用你自己域名下的 Google Workspace，批量创建邮箱，并统一管理安全策略。
所有操作都走官方 Admin SDK Directory API，账号完全归你的组织所有。

## 一次性准备（约 15 分钟）

1. **开通 Workspace**，并验证你的域名（如 `example.com`）。
2. **在 Google Cloud 控制台**新建一个项目，然后启用 **Admin SDK API**。
3. **创建服务账号** → 生成 JSON 密钥，保存为 `service-account.json`（已加入 .gitignore，切勿提交）。
4. **配置全网域委派**：进入 Admin 控制台 → 安全 → 访问权限和数据控制 → API 控制 → 全网域委派 → 添加新的客户端。
   填入服务账号的 Client ID，以及以下范围：
   ```
   https://www.googleapis.com/auth/admin.directory.user,https://www.googleapis.com/auth/admin.directory.user.security,https://www.googleapis.com/auth/admin.directory.orgunit
   ```
5. 安装依赖：
   ```bash
   python3 -m venv .venv && . .venv/bin/activate
   pip install -r requirements.txt
   ```

## 统一安全策略：OU + 管理后台策略

Workspace 的安全策略按组织部门（OU）继承。推荐的做法是：

1. 用脚本建一个专用 OU：
   ```bash
   python workspace_admin.py --admin admin@example.com create-ou /Staff/Enforce2SV
   ```
2. 在 Admin 控制台里给这个 OU 配置策略（这些策略目前无法通过 Directory API 写入，只能在后台设置，但**一次设置，整个 OU 自动生效**）：
   - **安全 → 身份验证 → 两步验证**：允许用户开启 → **强制执行**：开启，并设置新用户注册宽限期（如 1 周）。
     方式可选“仅限安全密钥”或“除短信外的任何方式”，更安全。
   - **安全 → 身份验证 → 密码管理**：设置最小长度 ≥ 12、启用强密码、禁止重复使用。
   - **安全 → 登录质询**：开启员工 ID 登录质询。
   - **安全 → 访问权限和数据控制 → 低安全性应用 / 第三方应用访问权限**：按需限制。
3. 以后新建的账号直接放进这个 OU，就会自动继承上述策略。

## 常用命令

所有写操作都可以先加 `--dry-run` 预览。

```bash
A="--admin admin@example.com --key service-account.json"

# 批量建号（CSV 模板见 users.example.csv），每个账号自动生成 16 位强密码，
# 并要求首次登录修改密码。初始密码写入 created_users.csv（权限 600）
python workspace_admin.py $A create-users users.example.csv --org-unit /Staff/Enforce2SV

# 已有账号批量移入强制 2SV 的 OU
python workspace_admin.py $A move-ou emails.txt /Staff/Enforce2SV

# 安全审计：导出每个账号的 2SV 注册/强制状态、辅助邮箱/手机是否设置、最后登录时间
python workspace_admin.py $A audit --org-unit /Staff --out audit.csv

# 批量重置密码，并强制所有设备登出（比如员工离职或怀疑泄露时使用）
python workspace_admin.py $A reset-password emails.txt --signout

# 批量停用 / 恢复 / 强制登出
python workspace_admin.py $A suspend emails.txt
python workspace_admin.py $A unsuspend emails.txt
python workspace_admin.py $A signout emails.txt
```

`emails.txt` 可以是每行一个邮箱的纯文本，也可以是带 `primaryEmail` 列的 CSV（例如 audit.csv 筛选后的结果）。

## 注意事项

- **2SV 需要用户本人完成注册**（绑定验证器 App 或安全密钥），管理员无法代为设置。强制策略加宽限期可以确保每个人在期限内完成。
  如果用户丢了设备，管理员可以在 Admin 控制台为其生成**备用验证码**。
- **新开通的域名有建号速率限制**，数百个账号建议分批创建。脚本已内置请求间隔（`--pause`）和指数退避重试。
- 输出的 `created_users.csv` / `reset_passwords.csv` 含明文密码。请通过安全渠道分发，分发后立即删除。
- 服务账号密钥权限很大，应妥善保管，并定期轮换。
