<template>
  <div class="site-shell">
    <header><b class="logo">T</b><strong>东方fx天则</strong><span>登录　 注册</span></header>
    <main>
      <section class="hero">
        <p>当前主玩法　 FX战士</p>
        <h1>东方fx天则<br />FX战士</h1>
        <div class="description">
          用金圆券买卖幻想外币，跟随汇率变化做出判断。<br />查看汇率、管理持仓，在模拟市场中体验交易。
        </div>
        <div class="shell-buttons"><b>注册账号</b><b>立即登录</b></div>
      </section>
      <h2>外汇行情</h2>
      <section class="quote">
        <strong>市场行情</strong>
        <div class="placeholder wide"></div>
        <div class="placeholder"></div>
        <div class="chart"></div>
      </section>
    </main>
  </div>
</template>

<style scoped>
.site-shell {
  min-height: 100%;
  background: #fff;
  color: #000;
  font-family: Arial, 'Microsoft YaHei', sans-serif;
}
header {
  height: 56px;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 30px;
  background: #000;
  color: #fff;
  font-size: 14px;
}
header span {
  margin-left: auto;
}
.logo {
  padding: 4px 8px;
  color: #000;
  background: #fff;
}
main {
  padding: 24px 4vw;
}
.hero {
  position: relative;
  padding: 24px 30px;
  border: 4px double #000;
}
.hero::after {
  content: '';
  position: absolute;
  right: 12px;
  top: 12px;
  width: 36px;
  height: 36px;
  border-top: 3px solid;
  border-right: 3px solid;
}
.hero p {
  margin: 0 0 16px;
  color: #666;
  font-size: 12px;
}
h1 {
  margin: 0 0 24px;
  font-size: clamp(28px, 3.8vw, 64px);
  line-height: 1.05;
}
.description {
  color: #444;
  font-size: 14px;
  line-height: 1.8;
}
.shell-buttons {
  display: flex;
  gap: 12px;
  margin-top: 24px;
}
.shell-buttons b {
  padding: 12px 24px;
  border: 2px solid;
  font-size: 13px;
}
.shell-buttons b:first-child {
  background: #000;
  color: #fff;
}
h2 {
  font-size: 18px;
  padding-bottom: 15px;
  border-bottom: 2px solid;
  margin: 28px 0 16px;
}
.quote {
  height: 230px;
  border: 2px solid;
  padding: 24px;
  position: relative;
}
.placeholder {
  width: 70px;
  height: 9px;
  background: #d8d8d8;
  margin-top: 15px;
}
.placeholder.wide {
  width: 130px;
  height: 28px;
}
.chart {
  position: absolute;
  bottom: 40px;
  left: 38%;
  width: 27%;
  height: 24%;
  border-bottom: 2px solid #ddd;
  clip-path: polygon(0 90%, 20% 70%, 40% 85%, 60% 50%, 80% 65%, 100% 20%, 100% 100%, 0 100%);
  background: #eee;
}
@media (max-width: 600px) {
  header {
    padding: 0 18px;
  }
  main {
    padding: 16px;
  }
  .hero {
    padding: 22px 18px;
  }
  .description {
    font-size: 12px;
  }
  .shell-buttons b {
    padding: 10px 16px;
  }
}
</style>
