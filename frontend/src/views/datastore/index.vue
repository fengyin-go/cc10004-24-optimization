<template>
  <section class="page" data-module="datastore">
    <header class="page-head">
      <div>
        <h2>数据集管理管理</h2>
        <p class="page-desc">维护数据集定义的输入、执行约束与结果记录，为数据集管理提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记数据集定义</button>
        <button class="btn" type="button" @click="exportRows">导出数据集管理清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button class="link" type="button" @click="openEdit(row)">编辑定义</button>
            <button class="link" type="button" @click="openVersions(row)">版本记录</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无数据集管理数据，可先登记数据集定义</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条数据集管理记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      <span v-if="infoMessage" class="ok-text">{{ infoMessage }}</span>
    </footer>

    <!-- 编辑定义：只追加新版本 -->
    <div v-if="editOpen" class="modal-mask" @click.self="editOpen = false">
      <div class="modal">
        <h3>编辑数据集定义</h3>
        <p class="hint">
          修改会生成新版本快照，历史版本与已引用旧版本的趋势预测训练轮次都不会被倒改。
        </p>
        <label v-for="field in contentFields" :key="field" class="form-row">
          <span>{{ field }}</span>
          <input v-model="form[field]" :placeholder="`请输入${field}`" />
        </label>
        <p v-if="formMessage" class="error-text">{{ formMessage }}</p>
        <div class="modal-actions">
          <button class="btn ghost" type="button" @click="editOpen = false">取消</button>
          <button class="btn primary" type="button" :disabled="submitting" @click="submitEdit">
            保存为新版本
          </button>
        </div>
      </div>
    </div>

    <!-- 版本记录 -->
    <div v-if="versionsOpen" class="modal-mask" @click.self="versionsOpen = false">
      <div class="modal wide">
        <h3>版本记录 · {{ versions[0]?.['数据集编号'] ?? '' }}</h3>
        <table class="data-table compact">
          <thead>
            <tr>
              <th>版本</th>
              <th>定义状态</th>
              <th>变更类型</th>
              <th>变更字段</th>
              <th>名称</th>
              <th>字段列表</th>
              <th>创建时间</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="version in versions" :key="version.id">
              <td>v{{ version.version_no }}</td>
              <td>{{ version['定义状态'] }}</td>
              <td>{{ version['变更类型'] }}</td>
              <td>{{ (version['变更字段'] ?? []).join('、') || '—' }}</td>
              <td>{{ version['数据集名称'] ?? '—' }}</td>
              <td>{{ version['字段列表'] ?? '—' }}</td>
              <td>{{ version['创建时间'] ?? '—' }}</td>
            </tr>
          </tbody>
        </table>
        <div class="modal-actions">
          <button class="btn ghost" type="button" @click="versionsOpen = false">关闭</button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import { fetchJson, request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/datastore'
const columns = ["数据集编号", "数据集名称", "来源表名", "字段列表", "更新策略", "数据采样比例", "生命周期天数", "缓存开关"]
const actions = ["提交审核", "生效发布", "废弃下线"]
const statuses = ["草稿", "待审核", "已生效", "已废弃"]
const stats = [{"label": "数据集定义总数", "value": 0}, {"label": "已生效数据集", "value": 0}, {"label": "待审核数据集", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const infoMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const contentFields = columns
const editOpen = ref(false)
const editingId = ref<number | null>(null)
const form = reactive<Record<string, string>>({})
const formMessage = ref('')
const submitting = ref(false)
const versionsOpen = ref(false)
const versions = ref<Row[]>([])

function openEdit(row: Row) {
  editingId.value = Number(row.id)
  formMessage.value = ''
  Object.keys(form).forEach((key) => delete form[key])
  contentFields.forEach((field) => { form[field] = String(row[field] ?? '') })
  editOpen.value = true
}

function openVersions(row: Row) {
  versions.value = []
  versionsOpen.value = true
  void fetchJson<{ items: Row[] }>(`${ENDPOINT}/${row.id}/versions`)
    .then((payload) => { versions.value = payload.items ?? [] })
    .catch(() => { infoMessage.value = '版本记录读取失败' })
}

async function submitEdit() {
  if (!editingId.value) return
  formMessage.value = ''
  submitting.value = true
  try {
    const response = await request(`${ENDPOINT}/${editingId.value}`, {
      method: 'PATCH',
      body: JSON.stringify({ values: { ...form } }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      formMessage.value = payload.message ?? '数据集定义未保存'
      return
    }
    editOpen.value = false
    infoMessage.value = payload.message
    await reload()
  } catch (error) {
    formMessage.value = error instanceof Error ? error.message : '数据集定义保存失败'
  } finally {
    submitting.value = false
  }
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '数据集定义登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  infoMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('数据集管理动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据集管理操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('数据集定义列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据集管理列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.modal-mask { position: fixed; inset: 0; background: rgba(15, 23, 42, .45); display: flex; align-items: flex-start; justify-content: center; padding: 40px 16px; overflow: auto; z-index: 20; }
.modal { background: #fff; border-radius: 10px; padding: 18px 20px; width: 640px; max-width: 96vw; }
.modal.wide { width: 900px; }
.modal h3 { margin: 0 0 6px; }
.hint { color: var(--muted); font-size: 12px; margin: 4px 0 12px; }
.form-row { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }
.form-row span { width: 96px; color: var(--muted); font-size: 13px; }
.form-row input { flex: 1; padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 12px; }
.compact th, .compact td { font-size: 12px; padding: 6px 8px; }
.ok-text { color: #116329; }
</style>
