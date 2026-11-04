# Văn Phòng 3D AI Agents — Quy Chuẩn Dựng Nhân Vật 3D & Khung Xương Đi Bộ (Chủ tịch Dũng)

Dự án **Văn Phòng 3D AI Agents & Quản Lý GPU VRAM (RTX 5070 Ti)** (`http://127.0.0.1:7890/`).
Mục tiêu: Code lại hệ thống hiển thị và điều khiển **Nhân vật 3D & Khung xương chuyển động (`walk` / `sit` / `stand` / `play`)** khớp 100% với **5 bộ nhân vật 3D** mà Chủ tịch Dũng đã chọn trong `D:\Blend\`:

---

## 1. Danh sách 5 Model 3D chuẩn (`/models/*.glb`) & Khung xương thống nhất

Tất cả 5 model `.glb` đều được rig trên cùng **Hệ khung xương Humanoid 17 xương chuẩn** (`hips`, `spine`, `chest`, `neck`, `head`, `upper_arm.L`, `forearm.L`, `hand.L`, `upper_arm.R`, `forearm.R`, `hand.R`, `thigh.L`, `shin.L`, `foot.L`, `thigh.R`, `shin.R`, `foot.R`), gốc tọa độ tại mặt đế giày (`z = 0` trong Blender → `y = 0` trong Three.js), mặt hướng về `+Z` trong Three.js:

| Nhân vật trong Văn Phòng | ID (`agent.id` / `familyId`) | Nguồn `D:\Blend` | File GLB (`/models/...`) | Các Animation Clip có sẵn trong GLB |
|---|---|---|---|---|
| **1. Chủ tịch Sếp Dũng** | `chairman` | `D:\Blend\Dung\Humano_01Business_01` | `/models/sep_dung.glb?v=5` | `sit_desk`, `walk`, `stand` |
| **2. Phu nhân Mẹ Win** | `me_win` | `D:\Blend\studiya.fbx` | `/models/me_win.glb?v=5` | `sit_read`, `walk`, `stand` |
| **3. Bé Mai (Con gái)** | `be_mai` | `D:\Blend\Mai\Little Kid1.dae` | `/models/mai.glb?v=5` | `play`, `walk`, `stand`, `sit` |
| **4. CEO Claude** | `ceo_claude` | `D:\Blend\claude\Tasha 5.2.blend` | `/models/claude.glb?v=5` | `sit_work`, `sit_relax`, `walk`, `stand` |
| **5. 8 Nhân sự AI** (`codex1`, `codex2`, `codex3`, `agy`, `muse1`, `muse2`, `cit_voice`, `aux_services`) | `codex1..3`, `agy`, `muse1..2`, `cit_voice`, `aux_services` | `D:\Blend\nhân sự\Beautiful Asian girl 5.2 LTS.blend` | `/models/nhan_su.glb?v=5` (fallback `/models/freya.glb`) | `sit_work`, `sit_relax`, `walk`, `stand` |

---

## 2. Nhiệm vụ của Claude Cloud (Opus 5.5)

**LƯU Ý QUAN TRỌNG VỀ TỐC ĐỘ GHI FILE TRÊN CLOUD:**
- Giữ mỗi file tạo mới **dưới 220 dòng** để công cụ `Write` trên Cloud chạy tức thì, không bị nghẽn.
- Sau khi tạo/cập nhật xong, chạy ngay `git add . && git commit -m "feat: 3d character rig & locomotion controller" && git push -u origin HEAD`.

### Công việc cụ thể cần thực hiện:

1. **Tạo module độc lập `web/character3d.js` (≤ 220 dòng)** — Bộ điều khiển Khung xương & Dáng đi 3D chuẩn cơ sinh học (Biomechanical 3D Character & Skeleton Controller):
   - Chuẩn hóa hàm `cloneSkinnedGltf(gltf)` hỗ trợ nhân bản đầy đủ `THREE.SkinnedMesh` + `THREE.Skeleton` + `boneInverses` cho mọi nhân vật.
   - **Đồng bộ tốc độ bước chân với vận tốc di chuyển thực tế (Stride-Locked Walk Cycle)**:
     - Khi nhân vật đi bộ với vận tốc `v` (m/s), tự động điều chỉnh `walkAction.timeScale = (v / strideSpeed)` (với `strideSpeed ≈ 0.72` m/s cho người lớn, `0.55` m/s cho Bé Mai) để bàn chân bám sàn 100%, không bị trượt chân (foot-sliding).
   - **Bộ tinh chỉnh khung xương hậu xử lý (Procedural Skeleton Post-Fixer — chạy sau `mixer.update(dt)`)**:
     - Tự động tìm các xương chính (`hips`, `spine`, `chest`, `neck`, `head`, `upper_arm.L/R`, `forearm.L/R`, `hand.L/R`, `thigh.L/R`, `shin.L/R`, `foot.L/R`).
     - Chống lỗi bước đi bị khập khiễng / cứng gối: làm mượt chuyển tiếp (smooth damping) góc xoay xương hông (`hips`), cột sống (`spine`/`chest`) và đầu (`head`) khi chuyển giữa `sit` ↔ `walk` ↔ `stand`.
     - Khi đi bộ (`walk`): bổ sung nhịp chuyển trọng tâm hông tự nhiên (pelvic sway nhẹ theo pha bước chân), giữ cổ và đầu nhìn tự nhiên theo hướng đi (`lookYaw`), hai tay vung mềm mại sát thân (không bị dang rộng chữ A hay chĩa thẳng tay ra trước bụng).
     - Khi ngồi (`sit_work` / `sit_relax` / `sit_desk` / `sit_read`): căn chỉnh chính xác độ cao `hips` khớp mặt đệm ghế để không bị lơ lửng hay xuyên ghế.
   - **Tích hợp đầy đủ nhân vật 3D Bé Mai (`/models/mai.glb?v=5`)**:
     - Thay thế hoàn toàn khối hình học Chibi cũ của `be_mai` (`familyActors`) bằng model 3D thật `/models/mai.glb?v=5` (tỷ lệ `scale: 0.56` phù hợp chiều cao bé gái).
     - Khi Bé Mai di chuyển giữa Nhà banh, Cầu trượt, Vòng quay ngựa gỗ và sang phòng Ba Dũng: tự động chuyển mượt giữa clip `walk` (khi đang bước đi) và clip `play` / `stand` (khi đang vui chơi / vẫy tay chào Ba Dũng & Mẹ Win).

2. **Tích hợp vào `web/index.html`**:
   - Nhúng `<script src="/character3d.js?v=5"></script>` vào `web/index.html` và kết nối với hệ thống `agentMeshes`, `VIP_WALKERS` (`chairman`, `me_win`), và `familyActors` (`be_mai`).
   - Cập nhật version URL của các model thành `?v=5` (`/models/sep_dung.glb?v=5`, `/models/me_win.glb?v=5`, `/models/mai.glb?v=5`, `/models/claude.glb?v=5`, `/models/nhan_su.glb?v=5`).
