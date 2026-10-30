# Drone / Gitea integration

本文件僅含虛構範例與介面約定，不保存正式站網址、組織、repository、主機或路徑。
整合對象為 Drone 2.x 風格的 API；實際版本與部署子路徑由管理員在部署時確認。

## Build API

以 organization、repository 與 build number 查詢：

```text
GET https://ci.example.com/drone/api/repos/ExampleVendor/Example_QC/builds/1234
Authorization: Bearer <service-token>
```

`drone.base_url` 保留部署子路徑，後面再接 `/api/repos/.../builds/...`。
Drone 服務 token 需能讀取全部納管 repositories；Gitea OAuth token 不直接當成 Drone token。
回應必須包含相符的 build number 與可辨認的 status。
401 / 403 / 404 / timeout / 無效 JSON 均禁止清理；404 不代表允許刪除。

## 狀態規則

| 狀態 | 清理規則 |
| --- | --- |
| pending / running | 禁止 |
| blocked / waiting_on_dependencies | 禁止，仍可能開始執行 |
| success / failure / error / killed / skipped / declined | 可進入授權、路徑與稽核檢查 |
| 其他、缺少、無法查詢 | 禁止 |

Gitea admin 亦適用相同規則。先通過組織或管理員授權，再查核目標的 Drone build。
佇列中的清理工作執行前再次查詢 Drone，不使用列表載入時的快取。
執行狀態不查 LSF jobs 或 OS processes；此設計不宣稱 Drone 查核與 filesystem 刪除具有跨系統原子性。

## Gitea 身分

使用本服務獨立的 OAuth application 與 session：

- 本人資料：`<gitea.base_url>/api/v1/user`，讀取 `id`、`login`、`is_admin`。
- 組織：`<gitea.base_url>/api/v1/user/orgs`，完整分頁讀取。
- 一般成員可查看與刪除所屬組織的納管紀錄，不限 build 原始觸發者。
- 網站 `is_admin=true` 可跨組織操作，但不能略過 Drone 或 filesystem 保護。
- 不繼承 Drone 的備援管理員例外；不把組織 owner 當成 Gitea 網站管理員。
- 登入與刪除前重新查核必要身分，失敗時停止操作。
- 稽核保存操作人的 Gitea ID、username、is_admin 與 operation ID。

實際 Gitea 版本、OAuth scopes 與回應需於 UAT 驗證。
