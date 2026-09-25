# ADR-0019: 图文笔记的产物用 `SingleFileArtifact.extra_paths` 表达，不开第五种 `media_source`

- **状态**：Accepted
- **日期**：2026-09-24
- **决策人**：用户 + Qoder（**待确认项**：本条动了 Locked 契约面 `MediaArtifact`）
- **相关**：ADR-0004（适配器 Protocol）、ADR-0006（数据模型）、ADR-0016（共用解析层）、
  `docs/specs/platform-adapter.md §2.4`、`docs/specs/data-model.md §2.3`、
  `docs/plans/v2.1-migration-plan.md` T2.1（"保留：图文笔记支持"）

## 背景

T2.1 要求小红书适配器"保留图文笔记支持"。V1 的做法（`download_xiaohongshu_latest.py::download_note_images`）
是把原图逐张落到 `notes/<id>/images/01.jpg…18.jpg`，一条笔记最多 18 张。

V2 这边一条作品的产物只有两种表达方式（`models/media.py`）：

| 类型 | 语义 | 落库 |
|---|---|---|
| `SingleFileArtifact` | 一个可播放的媒体文件 | `media_path` |
| `VideoAudioPairArtifact` | 未合并的 DASH 两条轨 | `media_path` + `media_aux_paths_json[0]` = 音频轨 |

一叠 JPG 两个都不是。而 `download_media()` 的返回类型是 `MediaArtifact`，
`tasks/collect.py::build_video_draft()` 无条件要求一个产物 —— 所以"图文笔记"这件事
**在今天的契约里没有一个诚实的说法**。硬套只有两条路，都会说谎：

- 报 `MediaDownloadError`：一条图文笔记被记成**采集失败**，而它什么都没失败。
  更糟的是 `collect._Tally.to_result()` 里 `downloaded==0 and failed>0` 直接把整轮判红 ——
  一个图文占多数的博主会让看板常年红着，而那正是 AGENTS.md §1.3 点名要防的形状。
- 只存封面（`cover_path`）不存原图：字段合法，但"这条笔记有 9 张图"这件事
  在库里没有任何痕迹，V1 迁过来的 18 张图也无处落。等于**静默少一半功能**。

## 选项

**A. 加第三种产物 `ImageSetArtifact`，并给 `MediaSource` 添一个字面量（如 `page_image_urls`）。**
类型上最干净。代价链是实测出来的：`MediaSource` 与 `storage.schema.MEDIA_SOURCES` 是
同一份清单的两处定义（`models/media.py:28` 的 docstring 明写着"改一处必须改两处"），
而 DB 侧那条 CHECK 是**建表时写进 DDL 的字面量列表** —— 加一个取值要 batch 重建 `videos` 表
（Alembic 0003），还要连带改：`tests/unit/storage/test_schema_types.py` 的枚举比对、
`EXPECTED_CHECKS` 那份约束快照、`docs/specs/data-model.md §2.3` 的列说明、
前端 `schema.d.ts` 里的 enum（`npm run gen:api`）、以及"每个 enum 查询参数前端都要有一份
逐字相等的名单"那条看护。
**为一叠图片付一次整表重建 + 六处连带**，而 `videos` 表是这台机器上唯一装着真实数据的表。

**B. 给 `SingleFileArtifact` 加一个可选字段 `extra_paths`，图文笔记的产物 = 第一张图当主文件 +
其余进 `extra_paths`，`has_audio=False` / `has_video=False`。**
不动任何字面量清单，不加迁移，`media_aux_paths_json` 现成就是"同一条作品的其他产物文件"这一列。

**C. 不做图文笔记的媒体，只做元数据。**
最省，但违背 T2.1 那一栏明写的"保留"，而且 V1 那批图就在 `downloads/xiaohongshu/` 里，
迁移脚本会面对"V1 有 18 张图、V2 一行都放不下"的第二次盘点。

## 决定

**选 B**，并把契约面扩成这样（`models/media.py`）：

```python
extra_paths: tuple[Path, ...] = ()
"""同一趟采集带回来的**其他**产物文件。今天唯一的使用者是小红书图文笔记的原图
（主文件是第一张，其余按页面上的顺序进这里）。"""
```

三个后果要一起认下来：

1. **`media_aux_paths_json` 的语义从"DASH 音频轨"扩成"同一条作品的其他产物文件"**。
   `docs/specs/data-model.md §2.3` 跟着改。这个扩张是有界的：读这一列的地方一共两处
   （`collect._artifact_fields` 写、`postprocess._audio_source` 读），
   而读的那一侧**不看这一列** —— 它走 `audio_path_of(artifact)`，那里由 `has_audio` 把关。
   所以"aux 里可能装的不是音频轨"这件事不会流进转写链。看护：
   `test_audio_source_never_comes_from_the_aux_list_of_an_image_note`。
2. **`has_video=False` 从此有了真值**（此前只有 `has_audio` 被用过）。前端播放器那一格
   （T6.5）必须先问它，不能拿"有没有 `media_path`"当"能不能播"的判据 ——
   一张 3 MB 的 JPG 是真的躺在 `media_path` 上，`<video>` 喂进去只会黑屏。

   > **2026-09-25 追记：这一条当天只做到了一半，而缺的正好是要命的那半。**
   > `has_video` 只存在于**产物对象**上：`build_video_draft` 没把它写进
   > `videos.metadata_json`，`models/video.Video` 也不暴露它，于是 `VideoDetail.tsx`
   > 只能按"`media_path` 有没有"决定挂不挂 `<video>` —— 而图文笔记那一列**恰好有值**。
   > 结果就是这一格自己警告过的形状：详情页挂出一个永不加载、也不报错的黑播放器。
   > 发现它靠的不是评审，是当天收到的第一条**真**图文笔记（T3.3）。
   > 现在三段补齐：入库写进 metadata、模型上给一个"metadata 优先 / 后缀回落，
   > 且回落与 `/api/videos/{id}/media` 的容器白名单同判据"的推导字段、播放器改问这一位。
   > **这条追记本身才是重点**：ADR 里"判据必须在字段上"这种话，如果只写了产物侧、
   > 没写"谁读它"，它就会安静地停在产物上 —— 而**字段没人读与字段不存在是同一件事**。
3. **主文件是"其中一张图"而不是"这条作品的代表文件"**，这是本方案最弱的一环，
   明说：图文笔记没有天然的主文件。选第一张是因为 V1 的 `images/01.jpg` 就是首图，
   而封面（`cover_path`）走的是另一条已经存在的列。

## 为什么不是 A（一句话版）

A 在类型上更对，但它要动的是**存储层的一条 CHECK 与五个跨语言契约面**，
换来的收益是"`type(artifact)` 能区分出图文"。而这个区分今天没有任何消费方：
`audio_path_of()`、`total_size_bytes()`、前端的播放判断，三个需要的都是
"有没有音频 / 有没有视频 / 一共多少字节"，`has_audio` + `has_video` + `extra_paths`
三个字段已经把答案给全了。**等出现第一个真正需要"这是图文"这个类型判据的消费方，
再按 A 做一次，并且那时它应该顺带把 `page_image_urls` 这个来源值一起收进清单。**

## 后果

- `MediaArtifact` 仍是判别联合，但 `SingleFileArtifact` 不再承诺"只有一个文件"。
  凡是"扫一眼 `artifact.path` 就算知道了这条作品有什么"的代码从此不可信 ——
  包括 `postprocess` 与未来的导出。这是本次扩张的**真实代价**，写在这里而不是藏在字段注释里。
- 迁移侧：V1 那批 `images/*.jpg` 从此有地方放（`media_path` + `media_aux_paths_json`），
  `tools/migrate_from_v1.py` 的媒体盘点不用再为小红书图文单开一条路（T2.1 落地之后）。
- 如果将来选 A，`extra_paths` 的迁移是**无损**的：把每个 `kind=="single_file" and not has_video`
  的行改写成 `image_set` 即可，不需要重建表之外的动作。
