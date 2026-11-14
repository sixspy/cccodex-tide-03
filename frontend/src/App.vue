<script setup>
import { computed, ref } from "vue";

const token = ref(localStorage.getItem("tide.token") || "");
const profile = ref(JSON.parse(localStorage.getItem("tide.profile") || "null"));
const username = ref("admin");
const password = ref("123456");
const loginError = ref("");
const batches = ref([]);
const current = ref(null);
const title = ref("");
const rawText = ref("");
const formError = ref("");
const busy = ref(false);

// 去重设置与本批声明的处理方式
const setting = ref(null);
const policy = ref("reject");
const settingMsg = ref("");
const settingBusy = ref(false);

// 重复列表与时刻详情
const duplicates = ref([]);
const detail = ref(null);
const view = ref("batches"); // batches | duplicates

const canWrite = computed(() => profile.value && profile.value.role === "writer");
const rejectDupes = computed(() => duplicates.value.filter((d) => d.section === "reject"));
const overwriteDupes = computed(() => duplicates.value.filter((d) => d.section === "overwrite"));

async function request(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (token.value) headers.Authorization = `Bearer ${token.value}`;
  const response = await fetch(path, { ...options, headers });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = typeof data.detail === "string"
      ? data.detail
      : (data.detail?.message || "请求没有完成");
    const error = new Error(message);
    error.payload = data.detail;
    throw error;
  }
  return data;
}

async function refresh() {
  batches.value = await request("/api/batches");
  await loadSetting();
  duplicates.value = await request("/api/duplicates");
  if (!current.value && batches.value.length) {
    await openBatch(batches.value[0].id);
  }
}

async function loadSetting() {
  setting.value = await request("/api/dedup-setting");
  if (setting.value.policy) policy.value = setting.value.policy;
}

async function saveSetting(nextPolicy) {
  if (!canWrite.value) return;
  settingMsg.value = "";
  settingBusy.value = true;
  try {
    setting.value = await request("/api/dedup-setting", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ policy: nextPolicy }),
    });
    policy.value = nextPolicy;
    settingMsg.value = "当日去重设置已更新";
  } catch (error) {
    settingMsg.value = error.message;
  } finally {
    settingBusy.value = false;
  }
}

async function openBatch(id) {
  view.value = "batches";
  detail.value = null;
  current.value = await request(`/api/batches/${id}`);
}

async function openDuplicate(row) {
  view.value = "duplicates";
  detail.value = await request(
    `/api/duplicates/${row.station_code}/${row.observed_at.replace(/[-: ]/g, "")}`
  );
}

async function submitLogin() {
  loginError.value = "";
  try {
    const data = await request("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: username.value, password: password.value }),
    });
    token.value = data.access_token;
    profile.value = { username: data.username, role: data.role, title: data.title };
    localStorage.setItem("tide.token", token.value);
    localStorage.setItem("tide.profile", JSON.stringify(profile.value));
    await refresh();
  } catch (error) {
    loginError.value = error.message;
  }
}

async function submitBatch() {
  formError.value = "";
  busy.value = true;
  try {
    const data = await request("/api/batches", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title: title.value,
        raw_text: rawText.value,
        duplicate_policy: policy.value,
      }),
    });
    title.value = "";
    rawText.value = "";
    await refresh();
    current.value = data;
    view.value = "batches";
  } catch (error) {
    formError.value = error.message;
    // 时刻处理不符：服务端已停下并落了未接纳批次，拉回那份批次详情
    if (error.payload?.batch) {
      current.value = error.payload.batch;
      view.value = "batches";
      await refresh();
      current.value = error.payload.batch;
    }
  } finally {
    busy.value = false;
  }
}

function logout() {
  token.value = "";
  profile.value = null;
  current.value = null;
  localStorage.removeItem("tide.token");
  localStorage.removeItem("tide.profile");
}

if (token.value) {
  refresh().catch(() => logout());
}
</script>

<template>
  <header class="top">
    <div>
      <p class="eyebrow">水文组 · 报文归集</p>
      <h1>潮位报文归集台</h1>
    </div>
    <div v-if="profile" class="who">
      <span>{{ profile.title }} {{ profile.username }}</span>
      <button class="ghost" @click="logout">离开</button>
    </div>
  </header>

  <main v-if="!profile" class="gate">
    <form @submit.prevent="submitLogin">
      <p>站码、时刻、潮位厘米、质量旗。一行一条，竖线分开。</p>
      <label>用户<input v-model="username" autocomplete="username" /></label>
      <label>口令<input v-model="password" type="password" autocomplete="current-password" /></label>
      <button>进入归集台</button>
      <p v-if="loginError" class="warn">{{ loginError }}</p>
    </form>
  </main>

  <main v-else class="desk">
    <section class="paste">
      <h2>贴入这一批</h2>
      <p class="rule">
        质量旗 A 实测，M 插补，B 可疑。同一站同一时刻再来时，按当日去重设置裁决。
      </p>
      <label>批次名<input v-model="title" :disabled="!canWrite" placeholder="例如 堡镇 11-13 午班" /></label>

      <article class="setting-card">
        <header>
          <h3>当日去重设置</h3>
          <span v-if="setting && setting.locked" class="tag tag-locked">当日已锁定</span>
          <span v-else class="tag tag-open">未锁定</span>
        </header>
        <p class="quiet" v-if="setting && setting.policy">
          当前：{{ setting.policy_label }} · {{ setting.setting_date }} · {{ setting.updated_by }} 设定
        </p>
        <p class="quiet" v-else>今天还没有归集，先选一套处理方式</p>
        <div class="policy-row">
          <button
            v-for="opt in (setting?.options || [])"
            :key="opt.value"
            type="button"
            class="policy"
            :class="{ on: policy === opt.value }"
            :disabled="!canWrite || settingBusy || (setting && setting.locked && setting.policy !== opt.value)"
            @click="saveSetting(opt.value)"
          >
            {{ opt.label }}
          </button>
        </div>
        <p class="quiet">
          拒绝重复：整行留在未接纳。覆盖厘米：新厘米覆盖旧值，详情保留覆盖前原值。
          当日随第一批报文锁定，今天不能再换。
        </p>
        <p v-if="settingMsg" class="warn">{{ settingMsg }}</p>
      </article>

      <label>本批处理方式
        <select v-model="policy" :disabled="!canWrite || (setting && setting.locked)">
          <option value="reject">拒绝重复（整行留未接纳）</option>
          <option value="overwrite">覆盖厘米（留覆盖前原值）</option>
        </select>
      </label>
      <textarea
        v-model="rawText"
        :disabled="!canWrite"
        placeholder="BZ03|202611130900|176|A"
        spellcheck="false"
      />
      <button :disabled="!canWrite || busy" @click="submitBatch">归集这一批</button>
      <p v-if="!canWrite" class="quiet">档案员只能翻已落入的报文。</p>
      <p v-if="formError" class="warn">{{ formError }}</p>
    </section>

    <section class="slips">
      <nav class="tabs">
        <button :class="{ on: view === 'batches' }" @click="view = 'batches'">已收批次</button>
        <button :class="{ on: view === 'duplicates' }" @click="view = 'duplicates'">
          重复列表<span class="count">{{ duplicates.length }}</span>
        </button>
      </nav>

      <template v-if="view === 'batches'">
        <button
          v-for="batch in batches"
          :key="batch.id"
          class="slip"
          :class="{ on: current && current.batch.id === batch.id }"
          @click="openBatch(batch.id)"
        >
          <strong>{{ batch.title }}</strong>
          <em>{{ batch.status }}</em>
          <span>落入 {{ batch.accepted_count }} 行 · 覆盖 {{ batch.overwrite_count }} 行 · 未接纳 {{ batch.rejected_count }} 行</span>
        </button>

        <article v-if="current" class="tape">
          <header>
            <h3>{{ current.batch.title }}</h3>
            <span>{{ current.batch.status }}</span>
          </header>
          <h4>已落库</h4>
          <table>
            <thead>
              <tr><th>行</th><th>站</th><th>时刻</th><th>厘米</th><th>旗</th></tr>
            </thead>
            <tbody>
              <tr v-for="row in current.landed" :key="row.line_no">
                <td>{{ row.line_no }}</td>
                <td>{{ row.station_code }}</td>
                <td>{{ row.observed_at }}</td>
                <td>{{ row.tide_cm }}</td>
                <td>{{ row.flag_label }}</td>
              </tr>
              <tr v-if="!current.landed.length"><td colspan="5">这一批没有落入的行</td></tr>
            </tbody>
          </table>

          <h4 v-if="current.overwrites.length">覆盖对照（覆盖前 → 覆盖后）</h4>
          <table v-if="current.overwrites.length">
            <thead>
              <tr><th>行</th><th>站</th><th>时刻</th><th>旧厘米</th><th>新厘米</th><th>旗</th></tr>
            </thead>
            <tbody>
              <tr v-for="row in current.overwrites" :key="row.line_no" class="row-overwrite">
                <td>{{ row.line_no }}</td>
                <td>{{ row.station_code }}</td>
                <td>{{ row.observed_at }}</td>
                <td class="old-cm">{{ row.old_tide_cm }}</td>
                <td class="new-cm">{{ row.new_tide_cm }}</td>
                <td>{{ row.flag_label }}</td>
              </tr>
            </tbody>
          </table>

          <h4>未接纳</h4>
          <ul>
            <li v-for="row in current.rejected" :key="row.line_no" :class="`kind-${row.kind}`">
              <code>{{ row.raw_line }}</code>
              <span class="reason">
                <em class="kind-tag">{{ row.kind_label }}</em>{{ row.reason }}
              </span>
            </li>
            <li v-if="!current.rejected.length">没有被挡下的行</li>
          </ul>
        </article>
      </template>

      <template v-else>
        <article v-if="detail" class="tape detail-card">
          <header>
            <h3>{{ detail.station_code }} · {{ detail.observed_at }}</h3>
            <button class="ghost small" @click="detail = null">返回列表</button>
          </header>

          <h4>新旧厘米对照</h4>
          <table v-if="detail.landed">
            <tbody>
              <tr><th>当前厘米</th><td class="new-cm">{{ detail.landed.tide_cm }}</td><td>{{ detail.landed.flag_label }}</td></tr>
            </tbody>
          </table>
          <p v-else class="quiet">这个站时刻还没有落库的行。</p>

          <h4 v-if="detail.history.length">覆盖留痕</h4>
          <table v-if="detail.history.length">
            <thead>
              <tr><th>批次行</th><th>覆盖前厘米</th><th>覆盖后厘米</th><th>旗</th><th>时间</th></tr>
            </thead>
            <tbody>
              <tr v-for="h in detail.history" :key="h.id" class="row-overwrite">
                <td>#{{ h.batch_id }}-{{ h.line_no }}</td>
                <td class="old-cm">{{ h.old_tide_cm }}</td>
                <td class="new-cm">{{ h.new_tide_cm }}</td>
                <td>{{ h.flag_label }}</td>
                <td>{{ h.created_at }}</td>
              </tr>
            </tbody>
          </table>

          <h4 v-if="detail.rejects.length">未接纳记录</h4>
          <ul v-if="detail.rejects.length">
            <li v-for="r in detail.rejects" :key="r.id" :class="`kind-${r.kind}`">
              <code>{{ r.raw_line }}</code>
              <span class="reason"><em class="kind-tag">{{ r.kind_label }}</em>{{ r.reason }}</span>
            </li>
          </ul>
        </article>

        <template v-else>
          <h4 class="section-title">覆盖区（{{ overwriteDupes.length }}）</h4>
          <table v-if="overwriteDupes.length" class="dupe-table">
            <thead>
              <tr><th>站码</th><th>时刻</th><th>覆盖次数</th><th>处理名</th></tr>
            </thead>
            <tbody>
              <tr v-for="row in overwriteDupes" :key="`${row.station_code}-${row.observed_at}`"
                  class="row-overwrite" @click="openDuplicate(row)">
                <td>{{ row.station_code }}</td>
                <td>{{ row.observed_at }}</td>
                <td>{{ row.overwrite_count }}</td>
                <td><em class="kind-tag kind-overwrite">{{ row.handling }}</em></td>
              </tr>
            </tbody>
          </table>
          <p v-else class="quiet">还没有覆盖记录。</p>

          <h4 class="section-title">拒绝区（{{ rejectDupes.length }}）</h4>
          <table v-if="rejectDupes.length" class="dupe-table">
            <thead>
              <tr><th>站码</th><th>时刻</th><th>覆盖次数</th><th>处理名</th></tr>
            </thead>
            <tbody>
              <tr v-for="row in rejectDupes" :key="`${row.station_code}-${row.observed_at}`"
                  @click="openDuplicate(row)">
                <td>{{ row.station_code }}</td>
                <td>{{ row.observed_at }}</td>
                <td>{{ row.overwrite_count }}</td>
                <td><em class="kind-tag" :class="`kind-${row.last_kind}`">{{ row.handling }}</em></td>
              </tr>
            </tbody>
          </table>
          <p v-else class="quiet">还没有拒绝记录。</p>
        </template>
      </template>
    </section>
  </main>
</template>

<style scoped>
.top, .desk, .gate { width: min(1180px, calc(100% - 32px)); margin: 0 auto; }
.top { display: flex; justify-content: space-between; align-items: end; padding: 28px 0 12px; }
.eyebrow { letter-spacing: 0.18em; text-transform: uppercase; color: #d7a15f; font-size: 12px; margin: 0 0 6px; }
h1 { margin: 0; font-weight: 560; font-size: 34px; }
h2, h3, h4 { margin: 0; }
.who { display: flex; gap: 12px; align-items: center; }
.ghost { background: transparent; color: #f3efe2; border: 1px solid rgba(243, 239, 226, 0.35); padding: 6px 12px; }
.ghost.small { color: #1c2430; border-color: rgba(28, 36, 48, 0.35); padding: 3px 8px; font-size: 12px; }
.gate { padding: 24px 0 48px; }
.gate form, .paste, .tape {
  background: #f3efe2;
  color: #1c2430;
  padding: 22px;
}
.gate form { width: min(420px, 100%); display: grid; gap: 12px; }
label { display: grid; gap: 6px; font-size: 14px; }
input, textarea, select {
  width: 100%;
  border: 0;
  background: #fffdf8;
  color: #1c2430;
  padding: 10px 12px;
  box-shadow: inset 0 0 0 1px #d9d0bc;
}
textarea { min-height: 220px; font-family: "IBM Plex Mono", Consolas, monospace; line-height: 1.6; }
button { background: #9d2c2c; color: #fffaf2; padding: 10px 14px; }
.desk { display: grid; grid-template-columns: 380px 1fr; gap: 22px; padding-bottom: 48px; }
.paste, .slips { display: grid; gap: 12px; align-content: start; }
.rule, .quiet { margin: 0; color: #5c5648; font-size: 13px; line-height: 1.5; }
.setting-card {
  background: #fffdf8;
  box-shadow: inset 0 0 0 1px #d9d0bc;
  padding: 14px;
  display: grid;
  gap: 8px;
}
.setting-card header { display: flex; justify-content: space-between; align-items: center; }
.tag { font-size: 12px; padding: 2px 8px; font-style: normal; }
.tag-locked { background: #9d2c2c; color: #fffaf2; }
.tag-open { background: #d9d0bc; color: #5c5648; }
.policy-row { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.policy {
  background: #f3efe2;
  color: #5c5648;
  box-shadow: inset 0 0 0 1px #d9d0bc;
  padding: 8px;
  font-size: 13px;
}
.policy.on { background: #1c2430; color: #f3efe2; box-shadow: none; }
.tabs { display: flex; gap: 8px; }
.tabs button {
  background: transparent;
  box-shadow: inset 0 0 0 1px rgba(243, 239, 226, 0.25);
  display: flex;
  gap: 8px;
  align-items: center;
}
.tabs button.on { background: #d7a15f; color: #1c2430; box-shadow: none; }
.count {
  background: rgba(243, 239, 226, 0.18);
  border-radius: 999px;
  padding: 0 8px;
  font-size: 12px;
}
.slip {
  text-align: left;
  background: transparent;
  color: #f3efe2;
  border: 1px solid rgba(243, 239, 226, 0.2);
  padding: 12px 14px;
  display: grid;
  gap: 4px;
}
.slip.on { border-color: #d7a15f; }
.slip em { font-style: normal; color: #d7a15f; }
.slip span, .tape li span { color: #5c5648; }
.tape header { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 8px; gap: 12px; }
.tape h4 { margin: 14px 0 8px; }
.section-title { color: #d7a15f; font-size: 15px; margin-top: 8px; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 6px 4px; border-bottom: 1px solid #e4dcc8; font-size: 14px; }
.dupe-table tr { cursor: pointer; }
.dupe-table tr:hover { background: #fffdf8; }
ul { list-style: none; padding: 0; margin: 0; display: grid; gap: 8px; }
li { display: grid; gap: 2px; }
code { font-family: Consolas, monospace; }
.warn { color: #9d2c2c; margin: 0; font-size: 13px; }
.kind-tag {
  font-style: normal;
  font-size: 12px;
  padding: 1px 7px;
  margin-right: 6px;
  background: #e4dcc8;
  color: #5c5648;
  white-space: nowrap;
}
.kind-overwrite, .kind-tag.kind-overwrite { background: #1f4d3a; color: #e8f3ec; }
.kind-tag.kind-contention { background: #8a5a17; color: #fff6e6; }
.kind-tag.kind-mismatch { background: #9d2c2c; color: #fffaf2; }
.row-overwrite td.new-cm { color: #1f4d3a; font-weight: 600; }
.row-overwrite td.old-cm { color: #8a7a5c; text-decoration: line-through; }
.old-cm { color: #8a7a5c; text-decoration: line-through; }
.new-cm { color: #1f4d3a; font-weight: 600; }
.reason { display: flex; align-items: baseline; }
.detail-card { display: grid; gap: 4px; }
@media (max-width: 900px) {
  .desk { grid-template-columns: 1fr; }
}
</style>
