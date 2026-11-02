# Traefik 子路徑部署

本文件與 `traefik.example.yml` 的所有主機名稱均為虛構資料。
正式設定請放在 repository 外，或本機 Git 忽略的 `config.yml`；不要提交正式網址。

## Web service 設定

```yaml
server:
  listen: "127.0.0.1"
  port: 9080
  public_url: "http://tools.example.com/qc_folder_mgt"
```

`public_url` 必須是瀏覽器經 Traefik 存取的完整外部網址，包含實際 port 與子路徑。
不要填後端連線位址。Gitea OAuth application 的 Redirect URI 必須完全對應：

```text
http://tools.example.com/qc_folder_mgt/auth/callback
```

登入後跳轉、cookie Path、UI 資產及 API URL 都使用這個設定。
程式不從 `Host` 或 `X-Forwarded-*` 推算 callback，避免代理 header 改變登入目標。
HTTP 部署使用非 Secure cookie；若外部改用 HTTPS，將 public_url 同步改成 HTTPS，cookie 會啟用 Secure。

## 自動註冊與移除

支援 [ObsidianDrake/Traefik-API](https://github.com/ObsidianDrake/Traefik-API) 的 Bearer token API：

```yaml
server:
  listen: "127.0.0.1"
  # port: 9080  # 不填、null 或 0 會自動分配；指定值則固定。
  public_url: "http://tools.example.com/qc_folder_mgt"
  data_dir: "./data"
traefik_api:
  enable: true
  base_url: "http://route-api.example.com:8000"
  token: "replace-with-traefik-api-token"
  route_name: "qc-folder-manager"
  upstream_host: "127.0.0.1"
  timeout_seconds: 30
```

`enable` 預設 `false`，此時不呼叫路由 API。啟用後：

1. 先綁定並保留 socket；自動 port 由 OS 分配，不先查詢再釋放。
2. 呼叫 `PUT /v1/routes/{route_name}`，upstream 為 `http://<upstream_host>:<實際 port>`。
3. `host`、`pathPrefix` 從 `server.public_url` 取得；無子路徑時 prefix 為 `/`。`stripPrefix: false`，`probePath` 為 `<base_path>/health`。
4. 註冊成功後開始 HTTP 請求處理與背景掃描；實際 port 及 API 回傳的 route 名稱寫入 log。
5. 正常結束、Ctrl+C 或 SIGTERM 時，先 GET 確認路由仍與此次註冊內容一致，再 DELETE API 回傳的名稱；成功為 204，已不存在的 404 也視為完成。最後釋放 listener 並等待背景工作結束。

`upstream_host` 是 Traefik 和 Traefik-API 都可連到的**本服務主機**，不含 scheme、port 或路徑；省略時沿用 `server.listen`。若 listen 是 `0.0.0.0` 或 `::`，必須另填可連線的 hostname/IP。三個服務不同機時不可填 `127.0.0.1`；請調整 listen、upstream_host，以及 Traefik-API 的 upstream allowlist。IPv6 upstream 請填未加方括號的地址，程式會組合正確的 URL。對外 `public_url` 的 Host 必須符合 Traefik-API 接受的 DNS/IPv4 格式。

`route_name` 預設 `qc-folder-manager`，僅接受小寫英數及 `-`，第一字元為英數、最多 63 字元。接管同入口舊設定時 API 可能保留舊名稱，因此刪除使用回傳的 `name`。`timeout_seconds` 預設 30 秒、範圍 1–300，部署時須涵蓋 API 設定的探測時間。HTTP(S) 連線不使用環境 proxy、不跟隨 redirect；HTTPS 使用正常憑證驗證。

註冊失敗（包含 401、409、逾時）會讓啟動回傳非零退出碼，不開始對外處理請求，也不刪除未成功註冊的既有 route。PUT 不自動重試，避免與 API 的接管語意衝突。`--check-config`／`--preflight` 不註冊、不刪除 route。`written` 僅代表 API 已寫入設定，不保證 Traefik 已載入。

關閉時若 API 不可用或 route 已被修改，會記錄清理失敗並以非零碼退出；不刪除已知不同的設定。API 沒有條件式 DELETE，因此 GET 與 DELETE 之間仍非原子操作，部署期間不要並行變更同一入口。SIGKILL、主機斷電無法執行清理；PUT 已寫入但回覆遺失時也可能留下 route，下一次啟動交由 API 探測與接管。需要持續的故障清理時，應由 API 或部署系統另設租約機制。

## 手動 Traefik 路由

[traefik.example.yml](traefik.example.yml) 使用 Traefik v2/v3 的 file provider 動態設定。
將副本放入既有 `providers.file.directory`，或合併到已設定的 file provider 檔案。
此模式請設 `traefik_api.enable: false` 並指定固定 `server.port`；不要同時為同一入口保留人工設定與 API 管理路由。
替換 Host、entryPoint 名稱與後端網址即可；不需要 Docker，也不改動現有 Gitea / Drone 路由。

範本保留 `/qc_folder_mgt` 後轉送給服務，不需 StripPrefix。
規則同時涵蓋無結尾斜線的首頁與其子路徑，避免錯誤匹配 `/qc_folder_mgt-other`。
若既有代理已使用 StripPrefix，程式也接受移除前綴的請求；`public_url` 仍必須含完整子路徑。

`127.0.0.1:9080` 僅適用 Traefik 和 binary 位於相同主機及 network namespace。
若在不同主機，將後端改為 Traefik 可連到的服務主機位址，並調整 `server.listen` 為相應內網介面；
後端連接埠僅開放給代理。對外 port 由現有 Traefik entryPoint 決定，後端 port 可維持 9080。

## 部署驗證

1. 透過外部 `/qc_folder_mgt/health` 檢查轉送。
2. 開啟 `/qc_folder_mgt`，確認 JS / CSS 由同一子路徑載入。
3. 完成 Gitea 登入，確認 callback 與登入後頁面都保留子路徑。
4. 驗證列表、掃描、刪除保護、稽核與登出；首次刪除使用可丟棄測試資料。

應用程式測試涵蓋保留及移除前綴兩種代理模式；實際 Traefik 設定仍需部署驗證。
參考官方 [File provider](https://doc.traefik.io/traefik/reference/install-configuration/providers/others/file/)、
[路由規則](https://doc.traefik.io/traefik/routing/routers/)、
[StripPrefix](https://doc.traefik.io/traefik/v3.3/middlewares/http/stripprefix/)。

