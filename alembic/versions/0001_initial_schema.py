"""V2.0 baseline schema (7 tables).

Revision ID: 0001
Revises:
Create Date: 2026-09-22 05:56:26.923802+00:00

生成方式（**照抄这条命令，别手写迁移**）：

    INTELLIGENCE_HUB_STORAGE__SQLITE_URL="sqlite+pysqlite:///<空库>" \
      python -X utf8 -m alembic -c alembic.ini revision --autogenerate \
      -m "initial schema" --rev-id 0001

两处必须知道的实现细节：

1. 时间列渲染成 `sa.DateTime()` 而不是 `UTCDateTime()` —— `alembic/env.py`
   的 `render_item` 钩子做的替换。DDL 完全等价（`UTCDateTime.impl is DateTime`），
   UTC 转换只发生在 Python 侧。少了那个钩子，这里会是一个**没 import 的全限定名**，
   `alembic revision` 成功而 `alembic upgrade` 直接 NameError（本机实测，docs/lessons.md 坑 8）。
2. 索引走 `op.batch_alter_table`（`render_as_batch=True`）—— SQLite 的
   ALTER TABLE 支持有限，非 batch 模式生成的迁移在 SQLite 上语法错。

**改这个文件之前先想清楚**：0001 已经在别人机器上跑过了吗？
V2.0 发布前没有，所以可以直接改；发布后**只能加 0002** ——
改 0001 会让已经迁移过的库停在错误的状态（版本号还是 0001，内容却不是）。

表的权威定义在 `src/intelligence_hub_v2/storage/schema.py`（`metadata`），
两边的一致性由 `check_schema_matches_migrations()` 守着（有测试）。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('platforms',
    sa.Column('name', sa.String(length=32), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('config_json', sa.Text(), nullable=False),
    sa.Column('health_status', sa.String(length=16), nullable=True),
    sa.Column('health_checked_at', sa.DateTime(), nullable=True),
    sa.Column('health_detail', sa.Text(), nullable=True),
    sa.CheckConstraint("health_status IS NULL OR health_status IN ('ok','degraded','unreachable','unknown')", name=op.f('ck_platforms_health_status_enum')),
    sa.CheckConstraint('enabled IN (0, 1)', name=op.f('ck_platforms_enabled_bool')),
    sa.PrimaryKeyConstraint('name', name=op.f('pk_platforms'))
    )
    op.create_table('task_runs',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('task_name', sa.String(length=64), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('params_json', sa.Text(), server_default='{}', nullable=False),
    sa.Column('config_snapshot_json', sa.Text(), server_default='{}', nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=False),
    sa.Column('ended_at', sa.DateTime(), nullable=True),
    sa.Column('summary_json', sa.Text(), nullable=True),
    sa.Column('manifest_path', sa.Text(), nullable=True),
    sa.Column('error_text', sa.Text(), nullable=True),
    sa.Column('progress', sa.Float(), server_default='0.0', nullable=False),
    sa.CheckConstraint("status = 'running' OR ended_at IS NOT NULL", name=op.f('ck_task_runs_terminal_has_ended_at')),
    sa.CheckConstraint("status IN ('running','success','partial','failed','timeout','cancelled')", name=op.f('ck_task_runs_status_enum')),
    sa.CheckConstraint('progress >= 0.0 AND progress <= 1.0', name=op.f('ck_task_runs_progress_range')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_task_runs'))
    )
    with op.batch_alter_table('task_runs', schema=None) as batch_op:
        batch_op.create_index('idx_task_runs_name', ['task_name'], unique=False)
        batch_op.create_index('idx_task_runs_started', ['started_at'], unique=False)
        batch_op.create_index('idx_task_runs_status', ['status'], unique=False)

    op.create_table('creators',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('platform', sa.String(length=32), nullable=False),
    sa.Column('platform_id', sa.String(length=191), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('avatar_url', sa.Text(), nullable=True),
    sa.Column('follower_count', sa.Integer(), nullable=True),
    sa.Column('profile_url', sa.Text(), nullable=False),
    sa.Column('is_tracking', sa.Boolean(), server_default='1', nullable=False),
    sa.Column('metadata_json', sa.Text(), server_default='{}', nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint('is_tracking IN (0, 1)', name=op.f('ck_creators_is_tracking_bool')),
    sa.ForeignKeyConstraint(['platform'], ['platforms.name'], name=op.f('fk_creators_platform_platforms'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_creators'))
    )
    with op.batch_alter_table('creators', schema=None) as batch_op:
        batch_op.create_index('idx_creators_platform', ['platform'], unique=False)
        batch_op.create_index('idx_creators_tracking', ['is_tracking'], unique=False)
        batch_op.create_index('uq_creators_platform_platform_id', ['platform', 'platform_id'], unique=True)

    op.create_table('manifests',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('task_id', sa.String(length=36), nullable=False),
    sa.Column('schema_version', sa.String(length=8), server_default='2.0', nullable=False),
    sa.Column('file_path', sa.Text(), nullable=False),
    sa.Column('written_at', sa.DateTime(), nullable=False),
    sa.Column('content_json', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['task_id'], ['task_runs.id'], name=op.f('fk_manifests_task_id_task_runs'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_manifests'))
    )
    with op.batch_alter_table('manifests', schema=None) as batch_op:
        batch_op.create_index('idx_manifests_task', ['task_id'], unique=False)
        batch_op.create_index('idx_manifests_written', ['written_at'], unique=False)

    op.create_table('task_events',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('task_id', sa.String(length=36), nullable=False),
    sa.Column('timestamp', sa.DateTime(), nullable=False),
    sa.Column('type', sa.String(length=64), nullable=False),
    sa.Column('payload_json', sa.Text(), server_default='{}', nullable=False),
    sa.ForeignKeyConstraint(['task_id'], ['task_runs.id'], name=op.f('fk_task_events_task_id_task_runs'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_task_events'))
    )
    with op.batch_alter_table('task_events', schema=None) as batch_op:
        batch_op.create_index('idx_task_events_task_time', ['task_id', 'timestamp'], unique=False)
        batch_op.create_index('idx_task_events_type_time', ['type', 'timestamp'], unique=False)

    op.create_table('videos',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('platform', sa.String(length=32), nullable=False),
    sa.Column('platform_video_id', sa.String(length=191), nullable=False),
    sa.Column('creator_id', sa.Integer(), nullable=True),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('published_at', sa.DateTime(), nullable=True),
    sa.Column('duration_seconds', sa.Float(), nullable=True),
    sa.Column('view_count', sa.Integer(), nullable=True),
    sa.Column('like_count', sa.Integer(), nullable=True),
    sa.Column('comment_count', sa.Integer(), nullable=True),
    sa.Column('share_count', sa.Integer(), nullable=True),
    sa.Column('media_path', sa.Text(), nullable=True),
    sa.Column('media_source', sa.String(length=32), nullable=True),
    sa.Column('media_aux_paths_json', sa.Text(), server_default='[]', nullable=False),
    sa.Column('cover_path', sa.Text(), nullable=True),
    sa.Column('metadata_json', sa.Text(), server_default='{}', nullable=False),
    sa.Column('is_hidden', sa.Boolean(), server_default='0', nullable=False),
    sa.Column('hidden_at', sa.DateTime(), nullable=True),
    sa.Column('hidden_reason', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("media_source IS NULL OR media_source IN ('yt_dlp','page_play_url','dash_merged','dash_split')", name=op.f('ck_videos_media_source_enum')),
    sa.CheckConstraint('is_hidden IN (0, 1)', name=op.f('ck_videos_is_hidden_bool')),
    sa.ForeignKeyConstraint(['creator_id'], ['creators.id'], name=op.f('fk_videos_creator_id_creators'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_videos'))
    )
    with op.batch_alter_table('videos', schema=None) as batch_op:
        batch_op.create_index('idx_videos_created', ['created_at'], unique=False)
        batch_op.create_index('idx_videos_creator', ['creator_id'], unique=False)
        batch_op.create_index('idx_videos_platform_published', ['platform', 'published_at'], unique=False)
        batch_op.create_index('idx_videos_visible', ['is_hidden', 'published_at'], unique=False)
        batch_op.create_index('uq_videos_platform_platform_video_id', ['platform', 'platform_video_id'], unique=True)

    op.create_table('transcripts',
    sa.Column('video_id', sa.Integer(), nullable=False),
    sa.Column('engine', sa.String(length=32), nullable=False),
    sa.Column('language', sa.String(length=16), nullable=True),
    sa.Column('char_count', sa.Integer(), nullable=False),
    sa.Column('sentence_count', sa.Integer(), nullable=False),
    sa.Column('text_path', sa.Text(), nullable=False),
    sa.Column('segments_json', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("engine IN ('sherpa_sense_voice','bilibili_subtitle','youtube_subtitle','manual')", name=op.f('ck_transcripts_engine_enum')),
    sa.CheckConstraint('char_count >= 0', name=op.f('ck_transcripts_char_count_nonneg')),
    sa.CheckConstraint('sentence_count >= 0', name=op.f('ck_transcripts_sentence_count_nonneg')),
    sa.ForeignKeyConstraint(['video_id'], ['videos.id'], name=op.f('fk_transcripts_video_id_videos'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('video_id', name=op.f('pk_transcripts'))
    )
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_table('transcripts')
    with op.batch_alter_table('videos', schema=None) as batch_op:
        batch_op.drop_index('uq_videos_platform_platform_video_id')
        batch_op.drop_index('idx_videos_visible')
        batch_op.drop_index('idx_videos_platform_published')
        batch_op.drop_index('idx_videos_creator')
        batch_op.drop_index('idx_videos_created')

    op.drop_table('videos')
    with op.batch_alter_table('task_events', schema=None) as batch_op:
        batch_op.drop_index('idx_task_events_type_time')
        batch_op.drop_index('idx_task_events_task_time')

    op.drop_table('task_events')
    with op.batch_alter_table('manifests', schema=None) as batch_op:
        batch_op.drop_index('idx_manifests_written')
        batch_op.drop_index('idx_manifests_task')

    op.drop_table('manifests')
    with op.batch_alter_table('creators', schema=None) as batch_op:
        batch_op.drop_index('uq_creators_platform_platform_id')
        batch_op.drop_index('idx_creators_tracking')
        batch_op.drop_index('idx_creators_platform')

    op.drop_table('creators')
    with op.batch_alter_table('task_runs', schema=None) as batch_op:
        batch_op.drop_index('idx_task_runs_status')
        batch_op.drop_index('idx_task_runs_started')
        batch_op.drop_index('idx_task_runs_name')

    op.drop_table('task_runs')
    op.drop_table('platforms')
    # ### end Alembic commands ###
