<template>
  <div class="pve-shell">
    <h1>PvE 机器人</h1>
    <nav aria-label="机器人管理">
      <RouterLink to="/admin/pve/lmsr">LMSR 机器人</RouterLink
      ><RouterLink to="/admin/pve/fx">FX 机器人</RouterLink>
    </nav>
    <RouterView />
  </div>
</template>
<style scoped>
.pve-shell {
  padding: 16px;
  max-width: 1400px;
  margin: auto;
}
h1 {
  font-size: 22px;
}
nav {
  display: flex;
  border-bottom: 2px solid #000;
  margin-bottom: 16px;
}
nav a {
  padding: 10px 18px;
  color: #000;
  font-weight: 700;
}
nav .router-link-active {
  background: #000;
  color: white;
}
</style>
