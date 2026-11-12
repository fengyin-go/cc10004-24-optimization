<template>
  <section class="page" data-module="forecast">
    <header class="page-head">
      <div>
        <h2>趋势预测管理</h2>
        <p class="page-desc">
          每次训练形成独立轮次，冻结训练定义与数据集版本；评估记录随轮次留存，
          保存评估与推进生效一步完成。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记预测模型</button>
        <button class="btn" type="button" @click="exportRows">导出趋势预测清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>模型编号</span>
        <input v-model="keyword" placeholder="按模型编号检索" />
      </label>
      <label class="filter-item">
        <span>状态</span>
        <select v-model="statusFilter">
          <option value="">全部</option>
          <option v-for="item in statuses" :key="item" :value="item">{{ item }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th>模型编号</th>
          <th>模型类型</th>
          <th>训练数据集</th>
          <th>数据集版本</th>
          <th>状态</th>
          <th>定义状态</th>
          <th>生效轮次</th>
          <th>生效评估</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td>{{ row['模型编号'] ?? '—' }}</td>
          <td>{{ row['模型类型'] ?? '—' }}</td>
          <td>{{ row['训练数据集'] ?? '—' }}</td>
          <td>{{ row['数据集版本'] ?? '—' }}</td>
          <td>
            <span class="pill" :class="statusClass(row)">{{ row.status }}</span>
          </td>
          <td>
            <span class="pill" :class="evidenceClass(row)">{{ row['证据状态'] ?? '未确认' }}</span>
          </td>
          <td>{{ row['生效轮次编号'] ?? '—' }}</td>
          <td>{{ evaluationText(row) }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="openDetail(row)">训练与评估</button>
            <button class="link" type="button" @click="runAction(row, '标记失效')">标记失效</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td colspan="9" class="empty-state">暂无趋势预测数据，可先登记预测模型</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条趋势预测记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <!-- 登记 / 修改训练定义 -->
    <div v-if="createOpen" class="modal-mask" @click.self="createOpen = false">
      <div class="modal">
        <h3>{{ editingId ? '修改训练定义' : '登记预测模型' }}</h3>
        <p class="hint">
          训练定义会写入轮次记录。修改定义不会倒改历史轮次与评估，只会影响之后的训练。
        </p>
        <label v-for="field in definitionFields" :key="field" class="form-row">
          <span>{{ field }}</span>
          <select v-if="field === '训练数据集'" v-model="form[field]">
            <option value="">请选择数据集</option>
            <option v-for="dataset in datasets" :key="dataset.id" :value="dataset['数据集编号']">
              {{ dataset['数据集编号'] }} · {{ dataset['数据集名称'] }}
            </option>
          </select>
          <input v-else v-model="form[field]" :placeholder="`请输入${field}`" />
        </label>
        <label class="form-row">
          <span>模型类型</span>
          <select v-model="form['模型类型']">
            <option value="时序回归">时序回归</option>
            <option value="指数平滑">指数平滑</option>
            <option value="梯度提升树">梯度提升树</option>
          </select>
        </label>
        <p v-if="formMessage" class="error-text">{{ formMessage }}</p>
        <div class="modal-actions">
          <button class="btn ghost" type="button" @click="createOpen = false">取消</button>
          <button class="btn primary" type="button" :disabled="submitting" @click="submitForm">
            {{ editingId ? '保存训练定义' : '登记模型' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 模型明细：训练轮次与评估 -->
    <div v-if="detailOpen" class="modal-mask" @click.self="detailOpen = false">
      <div class="modal wide">
        <h3>模型明细 · {{ detail?.['模型编号'] ?? '' }}</h3>
        <p v-if="detailMessage" class="error-text">{{ detailMessage }}</p>
        <p v-if="detailNote" class="ok-text">{{ detailNote }}</p>

        <template v-if="detail">
          <div class="detail-grid">
            <div><span class="muted">模型类型</span>{{ detail['模型类型'] ?? '—' }}</div>
            <div><span class="muted">状态</span>{{ detail.status }}</div>
            <div><span class="muted">训练数据集</span>{{ detail['训练数据集'] ?? '—' }}</div>
            <div><span class="muted">数据集版本</span>{{ detail['数据集版本'] ?? '—' }}</div>
            <div><span class="muted">目标字段</span>{{ detail['目标字段'] ?? '—' }}</div>
            <div><span class="muted">特征字段</span>{{ detail['特征字段'] ?? '—' }}</div>
            <div><span class="muted">预测步长</span>{{ detail['预测步长'] ?? '—' }}</div>
            <div><span class="muted">置信区间</span>{{ detail['置信区间'] ?? '—' }}</div>
            <div><span class="muted">定义状态</span>{{ detail['定义状态'] ?? '未确认' }}</div>
            <div><span class="muted">证据状态</span>{{ detail['证据状态'] ?? '未确认' }}</div>
          </div>
          <p v-if="(detail['缺失项'] ?? []).length" class="warn-text">
            存量迁移缺失项：{{ detail['缺失项'].join('、') }}；需补全并重新训练后才能生效。
          </p>
          <p v-if="detail['迁移说明']" class="muted">{{ detail['迁移说明'] }}</p>

          <section class="block">
            <h4>当前生效归属</h4>
            <div v-if="detail['生效轮次']" class="detail-grid">
              <div><span class="muted">生效轮次</span>{{ detail['生效轮次']['轮次编号'] }}</div>
              <div><span class="muted">数据集版本</span>{{ detail['生效轮次']['数据集版本'] }}</div>
              <div><span class="muted">预测步长</span>{{ detail['生效轮次']['预测步长'] }}</div>
              <div><span class="muted">置信区间</span>{{ detail['生效轮次']['置信区间'] }}</div>
              <div><span class="muted">评估指标</span>{{ detail['生效评估']?.['评估指标'] ?? '—' }}</div>
              <div><span class="muted">评估值</span>{{ detail['生效评估']?.['评估值'] ?? '—' }}</div>
              <div><span class="muted">生效时间</span>{{ detail['生效轮次']['生效时间'] ?? '—' }}</div>
              <div><span class="muted">评估编号</span>{{ detail['生效评估']?.['评估编号'] ?? '—' }}</div>
            </div>
            <p v-else class="muted">尚未有生效轮次；需先完成一轮训练、保存评估并生效。</p>
          </section>

          <section class="block">
            <h4>提交新训练轮次</h4>
            <div class="inline-form">
              <label class="filter-item">
                <span>训练数据集</span>
                <select v-model="roundForm['训练数据集']">
                  <option v-for="dataset in datasets" :key="dataset.id" :value="dataset['数据集编号']">
                    {{ dataset['数据集编号'] }} · {{ dataset['数据集名称'] }}（v{{ dataset['当前版本'] ?? 1 }}）
                  </option>
                </select>
              </label>
              <label v-for="field in ['目标字段', '特征字段', '预测步长', '置信区间']" :key="field" class="filter-item">
                <span>{{ field }}</span>
                <input v-model="roundForm[field]" />
              </label>
              <button class="btn primary" type="button" :disabled="submitting" @click="submitTraining">
                提交训练
              </button>
            </div>
            <p class="hint">提交后会冻结以上字段与所选数据集版本；同一时刻可以有多个轮次并行训练。</p>
          </section>

          <section class="block">
            <h4>训练轮次与评估</h4>
            <table class="data-table compact">
              <thead>
                <tr>
                  <th>轮次</th>
                  <th>序号</th>
                  <th>数据集版本</th>
                  <th>轮次状态</th>
                  <th>接纳结果</th>
                  <th>评估</th>
                  <th>裁决说明</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="round in detail['轮次列表']" :key="round['轮次编号']">
                  <td>{{ round['轮次编号'] }}</td>
                  <td>{{ round['轮次序号'] }}</td>
                  <td>{{ round['数据集版本'] }}</td>
                  <td>{{ round['轮次状态'] }}</td>
                  <td>{{ round['接纳结果'] ?? '—' }}</td>
                  <td>{{ evaluationOf(round) }}</td>
                  <td class="verdict">{{ round['裁决说明'] ?? '—' }}</td>
                  <td class="row-actions">
                    <button
                      v-if="round['轮次状态'] === '训练中'"
                      class="link"
                      type="button"
                      @click="completeRound(round)"
                    >
                      完成训练
                    </button>
                    <button
                      v-if="round['轮次状态'] === '训练完成'"
                      class="link"
                      type="button"
                      @click="selectEvaluationRound(round)"
                    >
                      录入评估
                    </button>
                  </td>
                </tr>
                <tr v-if="!(detail['轮次列表'] ?? []).length">
                  <td colspan="8" class="empty-state">还没有训练轮次，可先在上方提交训练</td>
                </tr>
              </tbody>
            </table>
          </section>

          <section v-if="evalRound" class="block">
            <h4>录入评估 · {{ evalRound['轮次编号'] }}</h4>
            <p class="hint">
              该轮次冻结：数据集 {{ evalRound['数据集版本'] }}、目标字段 {{ evalRound['目标字段'] }}、
              特征字段 {{ evalRound['特征字段'] }}、步长 {{ evalRound['预测步长'] }}、
              置信区间 {{ evalRound['置信区间'] }}。
            </p>
            <div class="inline-form">
              <label class="filter-item">
                <span>评估指标</span>
                <input v-model="evaluationForm['评估指标']" placeholder="如 MAE / RMSE / MAPE" />
              </label>
              <label class="filter-item">
                <span>评估值</span>
                <input v-model="evaluationForm['评估值']" placeholder="填写数值" />
              </label>
              <label class="filter-item grow">
                <span>结论</span>
                <input v-model="evaluationForm['结论']" placeholder="可选的评估结论" />
              </label>
              <button class="btn primary" type="button" :disabled="submitting" @click="saveEvaluation">
                保存评估并生效
              </button>
            </div>
            <p class="hint">评估保存与模型生效是一个不可拆的结果；落库失败时模型保持原生效版本。</p>
          </section>
        </template>
        <div class="modal-actions">
          <button class="btn ghost" type="button" @click="detailOpen = false">关闭</button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { fetchJson, request } from '@/api/client'

type Row = Record<string, any>
type Round = Record<string, any>

const ENDPOINT = '/api/forecast'
const statuses = ['草稿', '待训练', '已生效', '已失效']
const definitionFields = ['模型类型', '训练数据集', '目标字段', '特征字段', '预测步长', '置信区间']

const rows = ref<Row[]>([])
const datasets = ref<Row[]>([])
const total = ref(0)
const keyword = ref('')
const statusFilter = ref('')
const errorMessage = ref('')
const submitting = ref(false)

const stats = computed(() => [
  { label: '预测模型总数', value: rows.value.length },
  { label: '已生效模型', value: rows.value.filter((row) => row.status === '已生效').length },
  { label: '证据未确认', value: rows.value.filter((row) => row['证据状态'] !== '已确认').length },
])

const createOpen = ref(false)
const editingId = ref<number | null>(null)
const form = reactive<Record<string, string>>({})
const formMessage = ref('')

const detailOpen = ref(false)
const detail = ref<Row | null>(null)
const detailMessage = ref('')
const detailNote = ref('')
const roundForm = reactive<Record<string, string>>({})
const evalRound = ref<Round | null>(null)
const evaluationForm = reactive<Record<string, string>>({ '评估指标': '', '评估值': '', '结论': '' })

function statusClass(row: Row) {
  return { 'pill-active': row.status === '已生效', 'pill-warn': row.status === '待训练' }
}

function evidenceClass(row: Row) {
  return { 'pill-active': row['证据状态'] === '已确认', 'pill-danger': row['证据状态'] !== '已确认' }
}

function evaluationText(row: Row) {
  if (!row['生效评估记录']) return '—'
  const record = row['生效评估记录']
  return `${record['评估指标']} = ${record['评估值']}`
}

function evaluationOf(round: Round) {
  const found = (detail.value?.['评估列表'] ?? []).find((item: Row) => item['轮次编号'] === round['轮次编号'])
  return found ? `${found['评估指标']} = ${found['评估值']}` : '—'
}

function resetFilters() {
  keyword.value = ''
  statusFilter.value = ''
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  editingId.value = null
  formMessage.value = ''
  Object.keys(form).forEach((key) => delete form[key])
  definitionFields.forEach((field) => { form[field] = '' })
  createOpen.value = true
}

async function submitForm() {
  formMessage.value = ''
  submitting.value = true
  try {
    const url = editingId.value ? `${ENDPOINT}/${editingId.value}` : ENDPOINT
    const method = editingId.value ? 'PATCH' : 'POST'
    const response = await request(url, {
      method,
      body: JSON.stringify({ values: { ...form, ...(editingId.value ? {} : { 模型编号: nextModelCode() }) } }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      formMessage.value = payload.message ?? '训练定义未保存'
      return
    }
    createOpen.value = false
    detailNote.value = payload.message
    await reload()
    if (editingId.value) await reloadDetail(editingId.value)
  } catch (error) {
    formMessage.value = error instanceof Error ? error.message : '训练定义保存失败'
  } finally {
    submitting.value = false
  }
}

function nextModelCode() {
  let seq = rows.value.length + 1
  let candidate = `FORE-${String(seq).padStart(4, '0')}`
  while (rows.value.some((row) => row['模型编号'] === candidate)) {
    seq += 1
    candidate = `FORE-${String(seq).padStart(4, '0')}`
  }
  return candidate
}

async function openDetail(row: Row) {
  detailOpen.value = true
  detailNote.value = ''
  evalRound.value = null
  await reloadDetail(row.id)
  roundForm['训练数据集'] = detail.value?.['训练数据集'] ?? ''
  ;['目标字段', '特征字段', '预测步长', '置信区间'].forEach((field) => {
    roundForm[field] = detail.value?.[field] ?? ''
  })
}

async function reloadDetail(entryId: number) {
  detailMessage.value = ''
  try {
    detail.value = await fetchJson<Row>(`${ENDPOINT}/${entryId}`)
  } catch (error) {
    detailMessage.value = error instanceof Error ? error.message : '模型明细读取失败'
  }
}

async function completeRound(round: Round) {
  const entryId = detail.value?.id
  if (!entryId) return
  detailMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${entryId}/rounds/${round['轮次编号']}/complete`, {
      method: 'POST',
      body: JSON.stringify({}),
    })
    const payload = await response.json()
    if (!payload.ok) throw new Error(payload.message)
    detailNote.value = payload.message
    await reloadDetail(entryId)
    await reload()
  } catch (error) {
    detailMessage.value = error instanceof Error ? error.message : '完成训练失败'
  }
}

async function submitTraining() {
  const entryId = detail.value?.id
  if (!entryId) return
  detailMessage.value = ''
  submitting.value = true
  try {
    const response = await request(`${ENDPOINT}/${entryId}/rounds`, {
      method: 'POST',
      body: JSON.stringify({ values: { ...roundForm } }),
    })
    const payload = await response.json()
    if (!payload.ok) throw new Error(payload.message)
    detailNote.value = payload.message
    await reloadDetail(entryId)
    await reload()
  } catch (error) {
    detailMessage.value = error instanceof Error ? error.message : '提交训练失败'
  } finally {
    submitting.value = false
  }
}

function selectEvaluationRound(round: Round) {
  evalRound.value = round
  const found = (detail.value?.['评估列表'] ?? []).find((item: Row) => item['轮次编号'] === round['轮次编号'])
  evaluationForm['评估指标'] = found?.['评估指标'] ?? ''
  evaluationForm['评估值'] = found ? String(found['评估值'] ?? '') : ''
  evaluationForm['结论'] = found?.['结论'] ?? ''
}

async function saveEvaluation() {
  const entryId = detail.value?.id
  const round = evalRound.value
  if (!entryId || !round) return
  detailMessage.value = ''
  submitting.value = true
  try {
    const response = await request(`${ENDPOINT}/${entryId}/evaluations`, {
      method: 'POST',
      body: JSON.stringify({ round_no: round['轮次编号'], values: { ...evaluationForm } }),
    })
    const payload = await response.json()
    if (!payload.ok) throw new Error(payload.message)
    detailNote.value = payload.message
    evalRound.value = null
    await reloadDetail(entryId)
    await reload()
  } catch (error) {
    detailMessage.value = error instanceof Error ? error.message : '评估保存失败，模型保持原生效版本'
  } finally {
    submitting.value = false
  }
}

async function runAction(row: Row, action: string) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const payload = await response.json()
    if (!payload.ok) throw new Error(payload.message)
    await reload()
    if (detailOpen.value && detail.value?.id === row.id) await reloadDetail(row.id)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '趋势预测操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams()
  if (keyword.value) query.set('keyword', keyword.value)
  if (statusFilter.value) query.set('status', statusFilter.value)
  try {
    const payload = await fetchJson<{ items: Row[]; total: number }>(`${ENDPOINT}?${query.toString()}`)
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    rows.value = []
    errorMessage.value = error instanceof Error ? error.message : '趋势预测列表读取失败'
  }
}

async function reloadDatasets() {
  try {
    const payload = await fetchJson<{ items: Row[] }>('/api/datastore?size=200')
    datasets.value = payload.items ?? []
  } catch {
    datasets.value = []
  }
}

onMounted(async () => {
  await Promise.all([reload(), reloadDatasets()])
})
</script>

<style scoped>
.pill { display: inline-block; padding: 1px 8px; border-radius: 999px; background: #eef2f7; font-size: 12px; }
.pill-active { background: #e7f6ec; color: #116329; }
.pill-warn { background: #fff6e5; color: #9a6700; }
.pill-danger { background: #fde8e8; color: #b42318; }
.modal-mask { position: fixed; inset: 0; background: rgba(15, 23, 42, .45); display: flex; align-items: flex-start; justify-content: center; padding: 40px 16px; overflow: auto; z-index: 20; }
.modal { background: #fff; border-radius: 10px; padding: 18px 20px; width: 640px; max-width: 96vw; }
.modal.wide { width: 960px; }
.modal h3 { margin: 0 0 6px; }
.hint { color: var(--muted); font-size: 12px; margin: 4px 0 12px; }
.form-row { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }
.form-row span { width: 96px; color: var(--muted); font-size: 13px; }
.form-row input, .form-row select { flex: 1; padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 12px; }
.detail-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px 18px; font-size: 13px; }
.detail-grid .muted { display: block; color: var(--muted); font-size: 12px; }
.block { margin-top: 16px; border-top: 1px solid var(--border); padding-top: 12px; }
.block h4 { margin: 0 0 8px; }
.inline-form { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; }
.inline-form .grow { flex: 1; min-width: 200px; }
.inline-form input, .inline-form select { padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; }
.compact th, .compact td { font-size: 12px; padding: 6px 8px; }
.verdict { color: var(--muted); max-width: 260px; }
.ok-text { color: #116329; font-size: 12px; }
.warn-text { color: #9a6700; font-size: 12px; }
.muted { color: var(--muted); font-size: 12px; }
</style>
