<script setup>
import { onMounted, ref } from 'vue'
import { getJson, postJson } from '../api'

const domains = ref([])
const domainId = ref(1)
const batches = ref([])
const exports_ = ref([])
const viewRows = ref([])
const previewRows = ref([])
const previewBatch = ref(null)
const err = ref('')
const notice = ref('')
const busy = ref(false)

async function refresh() {
  const [b, e, v] = await Promise.all([
    getJson(`/api/mask/batches?domain_id=${domainId.value}`),
    getJson(`/api/mask/exports?domain_id=${domainId.value}`),
    getJson(`/api/mask/view?domain_id=${domainId.value}&limit=8`),
  ])
  batches.value = b
  exports_.value = e
  viewRows.value = v
}

onMounted(async () => {
  try {
    domains.value = await getJson('/api/domains')
    if (domains.value.length) domainId.value = domains.value[0].id
    await refresh()
  } catch (e) {
    err.value = String(e.message || e)
  }
})

async function snapshot() {
  busy.value = true
  err.value = ''
  notice.value = ''
  try {
    const b = await postJson('/api/mask/snapshots', { domain_id: domainId.value })
    notice.value = `批次 ${b.id} 已按规则 v${b.rule_version} 固化 ${b.snapshot_rows} 行`
    await refresh()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

async function preview(batch) {
  err.value = ''
  try {
    previewRows.value = await getJson(`/api/mask/batches/${batch.id}/rows`)
    previewBatch.value = batch
  } catch (e) {
    err.value = String(e.message || e)
  }
}

async function doExport(batch) {
  busy.value = true
  err.value = ''
  notice.value = ''
  try {
    const rec = await postJson('/api/mask/exports', {
      domain_id: domainId.value,
      batch_id: batch.id,
    })
    if (rec.status === '成功') {
      notice.value = `导出成功：${rec.file_path}（抽样 ${rec.sample_checked} 行通过）`
    } else {
      err.value = `导出失败（已失败闭，未落正式文件）：${rec.fail_reason}`
    }
    await refresh()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}
</script>
<template>
  <div>
    <h2>脱敏快照与对外导出</h2>
    <p class="lead">
      快照按「规则版本+批次」固化在独立脱敏快照表，与明文明细物理隔离；
      导出先写临时目录、抽样校验通过后原子改名落正式文件，检出明文即整次失败。
    </p>
    <div class="toolbar">
      <label>
        主题域
        <select v-model.number="domainId" @change="refresh">
          <option v-for="d in domains" :key="d.id" :value="d.id">{{ d.name }}</option>
        </select>
      </label>
      <button :disabled="busy" @click="snapshot">生成脱敏快照</button>
    </div>
    <p v-if="err" class="err">{{ err }}</p>
    <p v-if="notice" class="ok">{{ notice }}</p>

    <div class="panel">
      <h3>快照批次</h3>
      <table>
        <thead>
          <tr>
            <th>批次</th><th>规则版本</th><th>明细行数</th><th>快照行数</th>
            <th>状态</th><th>说明</th><th>生成时间</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="b in batches" :key="b.id">
            <td>{{ b.id }}</td>
            <td>v{{ b.rule_version }}</td>
            <td>{{ b.detail_rows }}</td>
            <td>{{ b.snapshot_rows }}</td>
            <td><span :class="b.status === '成功' ? 'ok' : 'warn'">{{ b.status }}</span></td>
            <td>{{ b.message }}</td>
            <td>{{ b.created_at }}</td>
            <td>
              <button class="secondary" :disabled="busy" @click="preview(b)">预览</button>
              <button class="secondary" :disabled="busy || b.status !== '成功'" @click="doExport(b)">导出</button>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-if="!batches.length" class="muted">暂无快照批次，请先「生成脱敏快照」。</p>
    </div>

    <div class="panel" v-if="previewBatch">
      <h3>批次 {{ previewBatch.id }} 快照行（规则 v{{ previewBatch.rule_version }}，已脱敏）</h3>
      <table>
        <thead>
          <tr>
            <th>编号</th><th>业务日</th><th>门店编码</th><th>门店名称（脱敏）</th>
            <th>品类</th><th>金额</th><th>件数</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in previewRows" :key="r.id">
            <td>{{ r.id }}</td>
            <td>{{ r.biz_day }}</td>
            <td>{{ r.store_code }}</td>
            <td>{{ r.store_name_masked }}</td>
            <td>{{ r.category }}</td>
            <td>{{ r.amount }}</td>
            <td>{{ r.qty }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel">
      <h3>导出记录</h3>
      <table>
        <thead>
          <tr>
            <th>编号</th><th>批次</th><th>规则版本</th><th>状态</th><th>失败原因</th>
            <th>快照行数</th><th>明细行数</th><th>抽样行数</th><th>内容哈希</th><th>时间</th><th>文件</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in exports_" :key="r.id">
            <td>{{ r.id }}</td>
            <td>{{ r.batch_id }}</td>
            <td>v{{ r.rule_version }}</td>
            <td><span :class="r.status === '成功' ? 'ok' : 'warn'">{{ r.status }}</span></td>
            <td>{{ r.fail_reason || '—' }}</td>
            <td>{{ r.snapshot_rows }}</td>
            <td>{{ r.detail_rows }}</td>
            <td>{{ r.sample_checked }}</td>
            <td class="hash">{{ r.content_hash ? r.content_hash.slice(0, 12) + '…' : '—' }}</td>
            <td>{{ r.created_at }}</td>
            <td>
              <a v-if="r.status === '成功'" :href="`/api/mask/exports/${r.id}/file`" download>下载</a>
              <span v-else>—</span>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-if="!exports_.length" class="muted">暂无导出记录。</p>
    </div>

    <div class="panel">
      <h3>内部只读脱敏视图（当前启用规则）</h3>
      <p class="muted">仅供内部查看，不是快照；层间对账与分层任务一律基于明文明细原文，不使用本视图。</p>
      <table>
        <thead>
          <tr>
            <th>编号</th><th>业务日</th><th>门店编码</th><th>门店名称（脱敏）</th>
            <th>品类</th><th>金额</th><th>件数</th><th>规则版本</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in viewRows" :key="r.id">
            <td>{{ r.id }}</td>
            <td>{{ r.biz_day }}</td>
            <td>{{ r.store_code }}</td>
            <td>{{ r.store_name_masked }}</td>
            <td>{{ r.category }}</td>
            <td>{{ r.amount }}</td>
            <td>{{ r.qty }}</td>
            <td>v{{ r.rule_version }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="!viewRows.length" class="muted">明细层为空或尚无启用规则。</p>
    </div>
  </div>
</template>
