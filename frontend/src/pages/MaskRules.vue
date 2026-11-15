<script setup>
import { onMounted, ref } from 'vue'
import { getJson, postJson } from '../api'

const rules = ref([])
const err = ref('')
const busy = ref(false)
const form = ref({ name: '', keep_head: 1, keep_tail: 1, mask_char: '*' })

async function refresh() {
  rules.value = await getJson('/api/mask/rules')
}

onMounted(async () => {
  try {
    await refresh()
  } catch (e) {
    err.value = String(e.message || e)
  }
})

async function create() {
  busy.value = true
  err.value = ''
  try {
    await postJson('/api/mask/rules', form.value)
    form.value = { name: '', keep_head: 1, keep_tail: 1, mask_char: '*' }
    await refresh()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

async function enable(version) {
  busy.value = true
  err.value = ''
  try {
    await postJson(`/api/mask/rules/${version}/enable`, {})
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
    <h2>脱敏规则</h2>
    <p class="lead">
      店名保留首尾、中间掩码。规则按版本固化：新建或切换启用版本只影响之后新生成的快照与导出，
      历史快照与历史导出保持原样，绝不改写。
    </p>
    <p v-if="err" class="err">{{ err }}</p>
    <div class="panel">
      <h3>新建规则版本</h3>
      <div class="toolbar">
        <label>规则名称 <input v-model.trim="form.name" placeholder="如：店名保留首尾中间掩码" /></label>
        <label>保留首字符数 <input type="number" min="0" v-model.number="form.keep_head" /></label>
        <label>保留尾字符数 <input type="number" min="0" v-model.number="form.keep_tail" /></label>
        <label>掩码字符 <input v-model="form.mask_char" maxlength="1" class="mask-char" /></label>
        <button :disabled="busy || !form.name" @click="create">新建并启用</button>
      </div>
      <p class="muted">示例：店名「城东店」按保留首 1 位、尾 1 位脱敏为「城*店」。新建版本自动成为当前启用版本。</p>
    </div>
    <div class="panel">
      <table>
        <thead>
          <tr>
            <th>版本</th><th>名称</th><th>保留首</th><th>保留尾</th><th>掩码字符</th>
            <th>状态</th><th>创建时间</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in rules" :key="r.id">
            <td>v{{ r.version }}</td>
            <td>{{ r.name }}</td>
            <td>{{ r.keep_head }}</td>
            <td>{{ r.keep_tail }}</td>
            <td>{{ r.mask_char }}</td>
            <td><span :class="r.enabled ? 'ok' : 'muted'">{{ r.enabled ? '当前启用' : '未启用' }}</span></td>
            <td>{{ r.created_at }}</td>
            <td>
              <button v-if="!r.enabled" class="secondary" :disabled="busy" @click="enable(r.version)">
                启用此版
              </button>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-if="!rules.length" class="muted">暂无规则。</p>
    </div>
  </div>
</template>
