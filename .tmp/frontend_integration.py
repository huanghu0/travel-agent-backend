from pathlib import Path

root = Path(r"D:\github-project\helloagents-trip-planner\frontend")
changes: dict[Path, str] = {}

def load(rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8")

def replace_once(text: str, old: str, new: str, rel: str) -> str:
    if text.count(old) != 1:
        raise RuntimeError(f"{rel}: expected one occurrence, found {text.count(old)} for {old[:80]!r}")
    return text.replace(old, new, 1)

# Shared-guide API contracts.
rel = "src/types/index.ts"
text = load(rel)
marker = "/** 当前登录用户；后端永远不会返回 password_hash。 */"
shared_types = '''/** 分享广场公开快照；后端已剔除用户标识、检索文本和索引内部字段。 */
export interface SharedTripRequestSnapshot {
  city: string
  travel_days: number
  transportation: string
  accommodation: string
  preferences: string[]
}

export interface SharedGuideSnapshot {
  request: SharedTripRequestSnapshot
  trip_plan: TripPlan
}

export type SharedGuideSort = 'latest' | 'popular'

export interface SharedGuideListItem {
  share_id: string
  title: string
  author_username: string
  city: string
  travel_days: number
  transportation: string
  preferences: string[]
  cover_image_url?: string | null
  quality_score?: number | null
  like_count: number
  published_at: string
  liked_by_me: boolean
}

export interface SharedGuideDetail extends SharedGuideListItem {
  snapshot: SharedGuideSnapshot
}

export interface SharedGuidePage {
  items: SharedGuideListItem[]
  next_cursor?: string | null
}

export interface OwnedSharedGuideListItem extends SharedGuideListItem {
  publication_status: string
  index_status: string
  last_index_error?: string | null
}

export interface OwnedSharedGuidePage {
  items: OwnedSharedGuideListItem[]
  next_cursor?: string | null
}

export interface LikeMutationResponse {
  liked: boolean
  like_count: number
}

'''
text = replace_once(text, marker, shared_types + marker, rel)
changes[root / rel] = text

# API methods, including all sharing endpoints and session deletion.
rel = "src/services/api.ts"
text = load(rel)
old_import = "  TripTaskCancelResponse,\n  TripTaskCreateResponse\n} from '@/types'"
new_import = "  TripTaskCancelResponse,\n  TripTaskCreateResponse,\n  LikeMutationResponse,\n  OwnedSharedGuideListItem,\n  OwnedSharedGuidePage,\n  SharedGuideDetail,\n  SharedGuideListItem,\n  SharedGuidePage,\n  SharedGuideSort\n} from '@/types'"
text = replace_once(text, old_import, new_import, rel)
old_error = """function getErrorMessage(error: unknown, fallback: string): string {
  if (axios.isAxiosError<{ detail?: string; message?: string }>(error)) {
    return error.response?.data?.detail || error.response?.data?.message || error.message || fallback
  }
  return error instanceof Error ? error.message : fallback
}
"""
new_error = """function getErrorMessage(error: unknown, fallback: string): string {
  if (axios.isAxiosError<{ detail?: string | Array<{ msg?: string }>; message?: string }>(error)) {
    const detail = error.response?.data?.detail
    if (typeof detail === 'string') return detail
    if (Array.isArray(detail)) {
      const validationMessage = detail.map((item) => item.msg).filter(Boolean).join('；')
      if (validationMessage) return validationMessage
    }
    return error.response?.data?.message || error.message || fallback
  }
  return error instanceof Error ? error.message : fallback
}
"""
text = replace_once(text, old_error, new_error, rel)
insert_before = "export default apiClient"
api_block = '''export interface SharedGuideListOptions {
  city?: string
  travel_days?: number
  transportation?: string
  sort?: SharedGuideSort
  limit?: number
  cursor?: string
}

/** 浏览分享广场；登录后响应会包含当前用户的点赞状态。 */
export async function listSharedGuides(options?: SharedGuideListOptions): Promise<SharedGuidePage> {
  try {
    const response = await apiClient.get<SharedGuidePage>('/api/shared-guides', { params: options })
    return response.data
  } catch (error: unknown) {
    throw new Error(getErrorMessage(error, '加载分享广场失败'))
  }
}

/** 读取公开分享快照，不暴露源会话、检索文本或索引内部字段。 */
export async function getSharedGuide(shareId: string): Promise<SharedGuideDetail> {
  try {
    const response = await apiClient.get<SharedGuideDetail>(`/api/shared-guides/${shareId}`)
    return response.data
  } catch (error: unknown) {
    throw new Error(getErrorMessage(error, '加载分享攻略失败'))
  }
}

/** 将当前确认版本发布到分享广场。 */
export async function shareTripSession(
  sessionId: string,
  title?: string | null
): Promise<OwnedSharedGuideListItem> {
  try {
    const response = await apiClient.post<OwnedSharedGuideListItem>(
      `/api/trip/sessions/${sessionId}/share`,
      { title: title?.trim() || null }
    )
    return response.data
  } catch (error: unknown) {
    throw new Error(getErrorMessage(error, '发布行程失败'))
  }
}

/** 查询当前用户已发布的攻略。 */
export async function listMySharedGuides(
  options?: SharedGuideListOptions
): Promise<OwnedSharedGuidePage> {
  try {
    const response = await apiClient.get<OwnedSharedGuidePage>('/api/users/me/shared-guides', {
      params: options
    })
    return response.data
  } catch (error: unknown) {
    throw new Error(getErrorMessage(error, '加载我的分享失败'))
  }
}

export async function updateSharedGuide(
  shareId: string,
  title?: string | null
): Promise<OwnedSharedGuideListItem> {
  try {
    const response = await apiClient.put<OwnedSharedGuideListItem>(
      `/api/shared-guides/${shareId}`,
      { title: title?.trim() || null }
    )
    return response.data
  } catch (error: unknown) {
    throw new Error(getErrorMessage(error, '更新分享攻略失败'))
  }
}

export async function deleteSharedGuide(shareId: string): Promise<void> {
  try {
    await apiClient.delete(`/api/shared-guides/${shareId}`)
  } catch (error: unknown) {
    throw new Error(getErrorMessage(error, '取消分享失败'))
  }
}

export async function setSharedGuideLike(
  shareId: string,
  liked: boolean
): Promise<LikeMutationResponse> {
  try {
    const response = liked
      ? await apiClient.put<LikeMutationResponse>(`/api/shared-guides/${shareId}/like`)
      : await apiClient.delete<LikeMutationResponse>(`/api/shared-guides/${shareId}/like`)
    return response.data
  } catch (error: unknown) {
    throw new Error(getErrorMessage(error, liked ? '点赞失败' : '取消点赞失败'))
  }
}

/** 永久删除当前用户的一条旅行规划会话及其任务、草稿和版本。 */
export async function deleteTripSession(sessionId: string): Promise<void> {
  try {
    await apiClient.delete(`/api/trip/sessions/${sessionId}`)
  } catch (error: unknown) {
    throw new Error(getErrorMessage(error, '删除旅行会话失败'))
  }
}

'''
text = replace_once(text, insert_before, api_block + insert_before, rel)
changes[root / rel] = text

# Router entry.
rel = "src/main.ts"
text = load(rel)
text = replace_once(text, "import Login from './views/Login.vue'", "import Login from './views/Login.vue'\nimport SharedGuides from './views/SharedGuides.vue'", rel)
route_marker = """    {
      path: '/history',
      name: 'History',
      component: History,
      meta: { requiresAuth: true }
    },
"""
route_replacement = route_marker + """    {
      path: '/shared-guides',
      name: 'SharedGuides',
      component: SharedGuides,
      meta: { requiresAuth: true }
    },
"""
text = replace_once(text, route_marker, route_replacement, rel)
changes[root / rel] = text

# Main navigation.
rel = "src/App.vue"
text = load(rel)
text = replace_once(text, "          <router-link to=\"/history\">历史行程</router-link>", "          <router-link to=\"/history\">历史行程</router-link>\n          <router-link to=\"/shared-guides\">分享广场</router-link>", rel)
changes[root / rel] = text

# Result-page publishing action.
rel = "src/views/Result.vue"
text = load(rel)
button_marker = """        <a-button v-if="tripPlan && !editMode" @click="toggleEditMode" type="default">
          ✏️ 编辑行程
        </a-button>
"""
button_new = """        <a-button
          v-if="tripPlan && !editMode && !isLocalDraft"
          :loading="sharing"
          @click="handleShare"
        >
          🌍 分享行程
        </a-button>
""" + button_marker
text = replace_once(text, button_marker, button_new, rel)
text = replace_once(text, "  resumeTripSession,\n  updateTripDraft", "  resumeTripSession,\n  shareTripSession,\n  updateTripDraft", rel)
text = replace_once(text, "const resuming = ref(false)\n", "const resuming = ref(false)\nconst sharing = ref(false)\n", rel)
go_history = """const goHistory = () => {
  router.push('/history')
}
"""
share_handler = go_history + """
const handleShare = async () => {
  if (!sessionId.value || !tripPlan.value) return
  sharing.value = true
  try {
    const shared = await shareTripSession(
      sessionId.value,
      `${tripPlan.value.city} · ${tripPlan.value.days.length}日行程`
    )
    message.success(`《${shared.title}》已发布到分享广场`)
  } catch (error) {
    message.error(error instanceof Error ? error.message : '发布行程失败')
  } finally {
    sharing.value = false
  }
}
"""
text = replace_once(text, go_history, share_handler, rel)
changes[root / rel] = text

# History session deletion and cancelled filter.
rel = "src/views/History.vue"
text = load(rel)
action_marker = """            <a-button @click="openResult(session.session_id)">查看行程</a-button>
            <a-button
"""
action_new = """            <a-button @click="openResult(session.session_id)">查看行程</a-button>
            <a-popconfirm
              title="确定永久删除这条会话吗？"
              description="关联任务、草稿和版本也会一并删除，且无法恢复。"
              ok-text="确认删除"
              cancel-text="取消"
              @confirm="removeSession(session.session_id)"
            >
              <a-button danger :loading="deletingSessionId === session.session_id">删除</a-button>
            </a-popconfirm>
            <a-button
"""
text = replace_once(text, action_marker, action_new, rel)
text = replace_once(text, "import { getTripSession, listTripSessions, resumeTripSession } from '@/services/api'", "import { deleteTripSession, getTripSession, listTripSessions, resumeTripSession } from '@/services/api'", rel)
text = replace_once(text, "const resumingSessionId = ref('')\n", "const resumingSessionId = ref('')\nconst deletingSessionId = ref('')\n", rel)
text = replace_once(text, "  'convergence_stopped'\n]", "  'convergence_stopped',\n  'cancelled'\n]", rel)
insert_on_mounted = "onMounted(loadSessions)"
remove_handler = """const removeSession = async (sessionId: string) => {
  deletingSessionId.value = sessionId
  try {
    await deleteTripSession(sessionId)
    sessions.value = sessions.value.filter((item) => item.session_id !== sessionId)
    if (selectedState.value?.session_id === sessionId) {
      detailOpen.value = false
      selectedState.value = null
    }
    message.success('旅行会话已永久删除')
  } catch (error) {
    message.error(error instanceof Error ? error.message : '删除会话失败')
  } finally {
    deletingSessionId.value = ''
  }
}

""" + insert_on_mounted
text = replace_once(text, insert_on_mounted, remove_handler, rel)
changes[root / rel] = text

# New share-square page.
share_view = r'''<template>
  <div class="share-page">
    <section class="share-hero">
      <div>
        <p class="eyebrow">COMMUNITY ATLAS</p>
        <h1>分享广场</h1>
        <p class="hero-copy">浏览真实行程快照，收藏灵感，也可以管理自己发布的旅行攻略。</p>
      </div>
      <div class="hero-actions">
        <button :class="{ active: activeTab === 'discover' }" type="button" @click="activeTab = 'discover'">
          发现攻略
        </button>
        <button :class="{ active: activeTab === 'mine' }" type="button" @click="activeTab = 'mine'">
          我的分享
        </button>
      </div>
    </section>

    <a-card :bordered="false" class="filter-card">
      <div class="filters">
        <a-input v-model:value="city" allow-clear placeholder="目的地城市" @press-enter="reload" />
        <a-input-number v-model:value="travelDays" :min="1" :max="30" placeholder="旅行天数" />
        <a-input v-model:value="transportation" allow-clear placeholder="交通方式" @press-enter="reload" />
        <a-select v-model:value="sort" style="width: 140px">
          <a-select-option value="latest">最新发布</a-select-option>
          <a-select-option value="popular">最多点赞</a-select-option>
        </a-select>
        <a-button type="primary" :loading="loading" @click="reload">筛选</a-button>
        <a-button @click="resetFilters">重置</a-button>
      </div>
    </a-card>

    <div v-if="loading && !currentItems.length" class="loading-panel">
      <a-spin size="large" tip="正在读取分享攻略..." />
    </div>

    <a-empty
      v-else-if="!currentItems.length"
      :description="activeTab === 'discover' ? '暂时还没有公开攻略' : '你还没有发布过攻略'"
      class="empty-panel"
    />

    <section v-else class="guide-grid">
      <article v-for="guide in currentItems" :key="guide.share_id" class="guide-card">
        <div class="cover" @click="openDetail(guide.share_id)">
          <img v-if="guide.cover_image_url" :src="guide.cover_image_url" :alt="guide.title" />
          <div v-else class="cover-fallback"><span>{{ guide.city.slice(0, 1) }}</span></div>
          <div class="cover-shade"></div>
          <div class="cover-meta">
            <span>{{ guide.city }}</span>
            <span>{{ guide.travel_days }} 天</span>
          </div>
        </div>

        <div class="card-body">
          <div class="title-row">
            <div>
              <h2 @click="openDetail(guide.share_id)">{{ guide.title }}</h2>
              <p>由 {{ guide.author_username }} 发布 · {{ formatDate(guide.published_at) }}</p>
            </div>
            <div v-if="guide.quality_score != null" class="quality-score">
              <strong>{{ guide.quality_score.toFixed(0) }}</strong><small>质量分</small>
            </div>
          </div>

          <div class="tag-row">
            <a-tag>{{ guide.transportation }}</a-tag>
            <a-tag v-for="preference in guide.preferences.slice(0, 3)" :key="preference" color="green">
              {{ preference }}
            </a-tag>
          </div>

          <div v-if="isOwnedGuide(guide)" class="index-row">
            <a-tag :color="indexColor(guide.index_status)">{{ indexLabel(guide.index_status) }}</a-tag>
            <span v-if="guide.last_index_error">索引将在后台重试</span>
          </div>

          <div class="card-actions">
            <a-button type="text" @click="openDetail(guide.share_id)">查看攻略</a-button>
            <template v-if="activeTab === 'discover'">
              <a-button
                :class="{ liked: guide.liked_by_me }"
                :loading="likingId === guide.share_id"
                @click="toggleLike(guide)"
              >
                {{ guide.liked_by_me ? '♥' : '♡' }} {{ guide.like_count }}
              </a-button>
            </template>
            <template v-else-if="isOwnedGuide(guide)">
              <a-button @click="startRename(guide)">修改标题</a-button>
              <a-button danger @click="confirmUnpublish(guide)">取消分享</a-button>
            </template>
          </div>
        </div>
      </article>
    </section>

    <div v-if="nextCursor" class="load-more">
      <a-button size="large" :loading="loadingMore" @click="loadMore">加载更多</a-button>
    </div>

    <a-drawer
      :open="detailOpen"
      :width="760"
      title="公开行程快照"
      @close="detailOpen = false"
    >
      <div v-if="detailLoading" class="drawer-loading"><a-spin size="large" /></div>
      <template v-else-if="detail">
        <div class="detail-heading">
          <div>
            <p>{{ detail.city }} · {{ detail.travel_days }} 天 · {{ detail.transportation }}</p>
            <h2>{{ detail.title }}</h2>
            <span>作者 {{ detail.author_username }}</span>
          </div>
          <a-button
            :class="{ liked: detail.liked_by_me }"
            :loading="likingId === detail.share_id"
            @click="toggleLike(detail)"
          >
            {{ detail.liked_by_me ? '♥ 已喜欢' : '♡ 喜欢' }} · {{ detail.like_count }}
          </a-button>
        </div>

        <a-alert
          type="info"
          show-icon
          message="这是发布时的只读快照"
          description="不会暴露作者的源会话 ID、自由文本要求、向量内容或索引内部信息。"
          class="snapshot-alert"
        />

        <section class="suggestion-block">
          <span>整体建议</span>
          <p>{{ detail.snapshot.trip_plan.overall_suggestions }}</p>
        </section>

        <article
          v-for="day in detail.snapshot.trip_plan.days"
          :key="`${day.day_index}-${day.date}`"
          class="day-card"
        >
          <header>
            <span>DAY {{ String(day.day_index + 1).padStart(2, '0') }}</span>
            <div><h3>{{ day.date }}</h3><p>{{ day.description }}</p></div>
          </header>
          <ol>
            <li v-for="attraction in day.attractions" :key="`${day.day_index}-${attraction.name}`">
              <strong>{{ attraction.name }}</strong>
              <span>{{ attraction.address }} · 建议 {{ attraction.visit_duration }} 分钟</span>
              <p>{{ attraction.description }}</p>
            </li>
          </ol>
          <footer v-if="day.hotel || day.meals.length">
            <span v-if="day.hotel">住宿：{{ day.hotel.name }}</span>
            <span v-if="day.meals.length">餐饮：{{ day.meals.map((meal) => meal.name).join('、') }}</span>
          </footer>
        </article>
      </template>
    </a-drawer>

    <a-modal
      v-model:open="renameOpen"
      title="修改分享标题"
      ok-text="保存并重新索引"
      cancel-text="取消"
      :confirm-loading="savingTitle"
      @ok="saveTitle"
    >
      <a-input v-model:value="renameTitle" :maxlength="200" show-count placeholder="输入攻略标题" />
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { message, Modal } from 'ant-design-vue'
import {
  deleteSharedGuide,
  getSharedGuide,
  listMySharedGuides,
  listSharedGuides,
  setSharedGuideLike,
  updateSharedGuide
} from '@/services/api'
import type {
  OwnedSharedGuideListItem,
  SharedGuideDetail,
  SharedGuideListItem,
  SharedGuideSort
} from '@/types'

type TabName = 'discover' | 'mine'

const activeTab = ref<TabName>('discover')
const city = ref('')
const travelDays = ref<number | null>(null)
const transportation = ref('')
const sort = ref<SharedGuideSort>('latest')
const publicItems = ref<SharedGuideListItem[]>([])
const ownedItems = ref<OwnedSharedGuideListItem[]>([])
const publicCursor = ref<string | null>(null)
const ownedCursor = ref<string | null>(null)
const loading = ref(false)
const loadingMore = ref(false)
const likingId = ref('')
const detailOpen = ref(false)
const detailLoading = ref(false)
const detail = ref<SharedGuideDetail | null>(null)
const renameOpen = ref(false)
const renameTitle = ref('')
const renamingGuide = ref<OwnedSharedGuideListItem | null>(null)
const savingTitle = ref(false)

const currentItems = computed(() => activeTab.value === 'discover' ? publicItems.value : ownedItems.value)
const nextCursor = computed(() => activeTab.value === 'discover' ? publicCursor.value : ownedCursor.value)

function queryOptions(cursor?: string | null) {
  return {
    city: city.value.trim() || undefined,
    travel_days: travelDays.value || undefined,
    transportation: transportation.value.trim() || undefined,
    sort: sort.value,
    limit: 12,
    cursor: cursor || undefined
  }
}

async function load(reset: boolean): Promise<void> {
  const cursor = reset ? null : nextCursor.value
  if (reset) loading.value = true
  else loadingMore.value = true
  try {
    if (activeTab.value === 'discover') {
      const page = await listSharedGuides(queryOptions(cursor))
      publicItems.value = reset ? page.items : [...publicItems.value, ...page.items]
      publicCursor.value = page.next_cursor || null
    } else {
      const page = await listMySharedGuides(queryOptions(cursor))
      ownedItems.value = reset ? page.items : [...ownedItems.value, ...page.items]
      ownedCursor.value = page.next_cursor || null
    }
  } catch (error) {
    message.error(error instanceof Error ? error.message : '分享列表加载失败')
  } finally {
    loading.value = false
    loadingMore.value = false
  }
}

function reload(): void {
  void load(true)
}

function loadMore(): void {
  if (nextCursor.value) void load(false)
}

function resetFilters(): void {
  city.value = ''
  travelDays.value = null
  transportation.value = ''
  sort.value = 'latest'
  reload()
}

async function openDetail(shareId: string): Promise<void> {
  detailOpen.value = true
  detailLoading.value = true
  detail.value = null
  try {
    detail.value = await getSharedGuide(shareId)
  } catch (error) {
    detailOpen.value = false
    message.error(error instanceof Error ? error.message : '分享攻略加载失败')
  } finally {
    detailLoading.value = false
  }
}

function applyLikeState(shareId: string, liked: boolean, likeCount: number): void {
  const update = (item: SharedGuideListItem) => {
    if (item.share_id === shareId) {
      item.liked_by_me = liked
      item.like_count = likeCount
    }
  }
  publicItems.value.forEach(update)
  ownedItems.value.forEach(update)
  if (detail.value?.share_id === shareId) update(detail.value)
}

async function toggleLike(guide: SharedGuideListItem): Promise<void> {
  likingId.value = guide.share_id
  try {
    const result = await setSharedGuideLike(guide.share_id, !guide.liked_by_me)
    applyLikeState(guide.share_id, result.liked, result.like_count)
  } catch (error) {
    message.error(error instanceof Error ? error.message : '点赞操作失败')
  } finally {
    likingId.value = ''
  }
}

function isOwnedGuide(guide: SharedGuideListItem): guide is OwnedSharedGuideListItem {
  return 'index_status' in guide
}

function startRename(guide: OwnedSharedGuideListItem): void {
  renamingGuide.value = guide
  renameTitle.value = guide.title
  renameOpen.value = true
}

async function saveTitle(): Promise<void> {
  if (!renamingGuide.value) return
  if (!renameTitle.value.trim()) {
    message.warning('分享标题不能为空')
    return
  }
  savingTitle.value = true
  try {
    const updated = await updateSharedGuide(renamingGuide.value.share_id, renameTitle.value)
    const index = ownedItems.value.findIndex((item) => item.share_id === updated.share_id)
    if (index >= 0) ownedItems.value[index] = updated
    renameOpen.value = false
    message.success('分享标题已更新，索引状态会自动同步')
  } catch (error) {
    message.error(error instanceof Error ? error.message : '更新分享失败')
  } finally {
    savingTitle.value = false
  }
}

function confirmUnpublish(guide: OwnedSharedGuideListItem): void {
  Modal.confirm({
    title: `取消分享《${guide.title}》？`,
    content: '攻略将从公开广场移除，相关向量索引也会同步删除。',
    okText: '取消分享',
    okType: 'danger',
    cancelText: '保留',
    async onOk() {
      await deleteSharedGuide(guide.share_id)
      ownedItems.value = ownedItems.value.filter((item) => item.share_id !== guide.share_id)
      publicItems.value = publicItems.value.filter((item) => item.share_id !== guide.share_id)
      if (detail.value?.share_id === guide.share_id) detailOpen.value = false
      message.success('分享已取消')
    }
  })
}

function formatDate(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', { month: 'short', day: 'numeric' }).format(date)
}

function indexLabel(status: string): string {
  return ({ READY: '检索就绪', PENDING: '等待索引', FAILED: '索引重试中', DELETING: '正在移除' } as Record<string, string>)[status] || status
}

function indexColor(status: string): string {
  return ({ READY: 'success', PENDING: 'processing', FAILED: 'warning', DELETING: 'default' } as Record<string, string>)[status] || 'default'
}

watch(activeTab, reload)
onMounted(reload)
</script>

<style scoped>
.share-page {
  --forest: #244e3f;
  --route: #e85d3f;
  min-height: calc(100vh - 116px);
  padding: 34px clamp(0px, 2vw, 28px) 70px;
  color: #172a25;
}
.share-hero { display: flex; align-items: flex-end; justify-content: space-between; gap: 28px; max-width: 1320px; margin: 0 auto 26px; padding: 26px 8px; }
.eyebrow { margin: 0; color: var(--route); font: 800 11px/1 sans-serif; letter-spacing: .22em; }
.share-hero h1 { margin: 12px 0 10px; font: 500 clamp(42px, 6vw, 74px)/.98 'Songti SC', Georgia, serif; letter-spacing: -.05em; }
.hero-copy { max-width: 680px; margin: 0; color: #66736d; font-size: 16px; line-height: 1.8; }
.hero-actions { display: flex; padding: 5px; border: 1px solid rgba(36,78,63,.14); border-radius: 999px; background: rgba(255,253,248,.8); }
.hero-actions button { min-width: 110px; padding: 11px 18px; border: 0; border-radius: 999px; color: #748078; background: transparent; cursor: pointer; font-weight: 700; }
.hero-actions button.active { color: #fff; background: var(--forest); box-shadow: 0 8px 20px rgba(36,78,63,.2); }
.filter-card { max-width: 1320px; margin: 0 auto 24px; border: 1px solid rgba(36,78,63,.08); background: rgba(255,253,248,.82); box-shadow: 0 16px 44px rgba(47,61,54,.07); }
.filters { display: grid; grid-template-columns: 1.2fr .7fr 1fr 140px auto auto; gap: 10px; }
.guide-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 22px; max-width: 1320px; margin: 0 auto; }
.guide-card { overflow: hidden; border: 1px solid rgba(36,78,63,.1); border-radius: 22px; background: #fffdf8; box-shadow: 0 18px 50px rgba(46,57,51,.08); transition: transform .25s ease, box-shadow .25s ease; }
.guide-card:hover { transform: translateY(-5px); box-shadow: 0 26px 64px rgba(46,57,51,.14); }
.cover { position: relative; height: 210px; overflow: hidden; cursor: pointer; background: #d9e2dc; }
.cover img { width: 100%; height: 100%; object-fit: cover; transition: transform .5s ease; }
.guide-card:hover .cover img { transform: scale(1.035); }
.cover-fallback { display: grid; width: 100%; height: 100%; place-items: center; background: radial-gradient(circle at 70% 20%, rgba(242,184,75,.82), transparent 28%), linear-gradient(145deg, #315d4d, #19372f); }
.cover-fallback span { color: rgba(255,255,255,.88); font: 500 84px/1 'Songti SC', serif; }
.cover-shade { position: absolute; inset: 0; background: linear-gradient(transparent 50%, rgba(8,24,19,.72)); }
.cover-meta { position: absolute; right: 17px; bottom: 15px; left: 17px; display: flex; justify-content: space-between; color: #fff; font-weight: 700; }
.card-body { padding: 20px; }
.title-row { display: grid; grid-template-columns: 1fr auto; gap: 14px; }
.title-row h2 { margin: 0; color: #18372e; font-size: 21px; line-height: 1.35; cursor: pointer; }
.title-row p { margin: 7px 0 0; color: #8a938e; font-size: 12px; }
.quality-score { display: grid; align-content: center; min-width: 52px; padding-left: 13px; border-left: 1px solid #e7e1d7; text-align: center; }
.quality-score strong { color: var(--route); font: 600 25px/1 Georgia, serif; }
.quality-score small { margin-top: 4px; color: #9a948b; font-size: 10px; }
.tag-row { min-height: 54px; padding: 17px 0 13px; }
.index-row { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; color: #8a938e; font-size: 12px; }
.card-actions { display: flex; align-items: center; gap: 8px; padding-top: 14px; border-top: 1px solid #eee8de; }
.card-actions .ant-btn:first-child { margin-right: auto; }
.liked { color: #c94438; border-color: rgba(201,68,56,.35); background: #fff2ef; }
.loading-panel, .empty-panel { display: grid; min-height: 360px; place-items: center; }
.load-more { padding: 34px 0 0; text-align: center; }
.drawer-loading { display: grid; min-height: 320px; place-items: center; }
.detail-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 24px; padding-bottom: 22px; border-bottom: 1px solid #ece5da; }
.detail-heading p { margin: 0 0 8px; color: var(--route); font: 700 12px/1 sans-serif; letter-spacing: .08em; }
.detail-heading h2 { margin: 0 0 8px; font: 500 30px/1.25 'Songti SC', serif; }
.detail-heading span { color: #7c8882; }
.snapshot-alert { margin: 22px 0; }
.suggestion-block { padding: 20px; background: #f6f2e9; border-left: 4px solid var(--forest); }
.suggestion-block span { color: var(--forest); font-weight: 800; }
.suggestion-block p { margin: 8px 0 0; color: #596761; line-height: 1.75; }
.day-card { margin-top: 22px; padding: 22px; border: 1px solid #e7e0d5; background: #fffdf8; }
.day-card header { display: grid; grid-template-columns: 72px 1fr; gap: 16px; }
.day-card header > span { color: var(--route); font: 700 13px/1.3 ui-monospace, monospace; }
.day-card h3 { margin: 0; font-size: 21px; }
.day-card header p { margin: 5px 0 0; color: #7a8680; }
.day-card ol { margin: 20px 0 0 17px; padding-left: 18px; border-left: 1px solid #d9d0c3; }
.day-card li { padding: 0 0 18px 10px; }
.day-card li strong, .day-card li span { display: block; }
.day-card li span { margin-top: 4px; color: #7b8680; font-size: 12px; }
.day-card li p { margin: 7px 0 0; color: #596761; line-height: 1.6; }
.day-card footer { display: flex; flex-wrap: wrap; gap: 12px 24px; padding-top: 16px; border-top: 1px dashed #ddd4c7; color: #65716b; font-size: 13px; }
@media (max-width: 1050px) { .guide-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } .filters { grid-template-columns: repeat(3, 1fr); } }
@media (max-width: 720px) { .share-page { padding: 20px 12px 50px; } .share-hero { align-items: flex-start; flex-direction: column; } .hero-actions { width: 100%; } .hero-actions button { flex: 1; } .filters, .guide-grid { grid-template-columns: 1fr; } .detail-heading { flex-direction: column; } }
</style>
'''
changes[root / "src/views/SharedGuides.vue"] = share_view

# Validate all changes before touching disk.
for path, content in changes.items():
    if not content.strip():
        raise RuntimeError(f"refusing to write empty file: {path}")

for path, content in changes.items():
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    print(path)
