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

## Traefik 路由

[traefik.example.yml](traefik.example.yml) 使用 Traefik v2/v3 的 file provider 動態設定。
將副本放入既有 `providers.file.directory`，或合併到已設定的 file provider 檔案。
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
