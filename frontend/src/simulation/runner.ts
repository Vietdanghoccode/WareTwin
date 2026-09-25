/**
 * Data-source switching (Phase 3). (Chuyển nguồn dữ liệu, Phase 3.)
 *  - Try the backend WebSocket first; once connected, store.twin is driven by FULL/PATCH and the local engine stops.
 *  - If it cannot connect or disconnects, switch to the local TypeScript engine and show LOCAL.
 *  - Control commands go through simControl() to the active data source.
 *  (Ưu tiên WebSocket backend; khi mất kết nối tự chuyển sang engine TypeScript local và hiển thị LOCAL.)
 */
import { useEffect } from "react";
import { SimEngine, SIM } from "./engine";
import { layout, useStore } from "../state/store";
import { wsConnect, wsDisconnect, wsSend } from "../services/ws";
import type { ScenarioInjection, TaskPriority, TaskType } from "../schema/twin_state";

let engine: SimEngine | null = null;
export function getEngine(): SimEngine {
  if (!engine) engine = new SimEngine(layout, { seed: useStore.getState().seed });
  return engine;
}
export function resetEngine(seed?: number) {
  engine = new SimEngine(layout, { seed: seed ?? useStore.getState().seed });
  useStore.getState().setTwin(engine.snapshot());
  return engine;
}

/** 統一的控制入口：online 走 WebSocket，否則操作本地引擎 */
export const simControl = {
  play(speed?: 1 | 2 | 5 | 10) {
    const st = useStore.getState();
    st.setPaused(false); if (speed) st.setSpeed(speed); else if (st.speed === 0) st.setSpeed(1);
    if (st.source === "online") wsSend({ type: "SIM_CONTROL", action: "PLAY", speed: speed ?? (st.speed === 0 ? 1 : st.speed) });
  },
  pause() {
    useStore.getState().setPaused(true);
    if (useStore.getState().source === "online") wsSend({ type: "SIM_CONTROL", action: "PAUSE" });
  },
  reset() {
    const st = useStore.getState();
    if (st.source === "online") wsSend({ type: "SIM_CONTROL", action: "RESET" }); else resetEngine();
  },
  inject(injection: ScenarioInjection) {
    const st = useStore.getState();
    if (st.source === "online") wsSend({ type: "INJECT", injection }); else getEngine().inject(injection);
  },
  createTask(task: { type: TaskType; priority: TaskPriority; source: string; destination: string; load_units?: number }) {
    const st = useStore.getState();
    if (st.source === "online") { wsSend({ type: "CREATE_TASK", task: { load_units: 1, ...task } }); return; }
    try { getEngine().createTask(task); } catch (e) { st.setNotice(`Task rejected: ${(e as Error).message}`); }
  },
  clearInjection(kind: ScenarioInjection["kind"], target_id: string) {
    const st = useStore.getState();
    if (st.source === "online") wsSend({ type: "CLEAR_INJECTION", kind, target_id }); else getEngine().clearInjection(kind, target_id);
  },
  ackAlert(alert_id: string) {
    const st = useStore.getState();
    if (st.source === "online") wsSend({ type: "ACK_ALERT", alert_id }); else getEngine().ackAlert(alert_id);
  },
};

export function useSimulationRunner() {
  useEffect(() => {
    const st = useStore.getState();
    let raf = 0, last = performance.now(), acc = 0;
    const MAX_TICKS_PER_FRAME = 40;

    // 本地引擎迴圈；只有 source !== "online" 時才推進
    const loop = (now: number) => {
      raf = requestAnimationFrame(loop);
      const dt = Math.min(0.25, (now - last) / 1000); last = now;
      const s = useStore.getState();
      if (s.source === "online") { acc = 0; return; }
      if (s.paused || s.speed === 0) return;
      const eng = getEngine();
      acc += dt * s.speed;
      let n = 0;
      while (acc >= SIM.TICK_S && n < MAX_TICKS_PER_FRAME) { eng.step(); acc -= SIM.TICK_S; n++; }
      if (n > 0) { eng.state.sim.speed = s.speed; eng.state.sim.mode = "LIVE"; s.setTwin(eng.snapshot()); }
    };

    // 先顯示本地引擎的初始畫面，避免連線期間空白
    st.setTwin(getEngine().snapshot());
    raf = requestAnimationFrame(loop);

    wsConnect((conn) => {
      const s = useStore.getState();
      if (conn === "online") {
        s.setSource("online"); s.setHeat(null);
      } else if (conn === "offline") {
        // 斷線：本地引擎從目前畫面的狀態接手 (保持連續)，避免畫面跳回 tick 0
        if (s.source === "online") { engine = new SimEngine(layout, { seed: s.seed, initialState: s.twin }); s.setHeat(null); }
        s.setSource("local");
      } else {
        if (s.source !== "online") s.setSource("connecting");
      }
    });
    // StrictMode（mount → cleanup → mount）或正常 unmount 時要把 WebSocket 與 listener 一起收掉，否則會留下重複連線
    return () => { cancelAnimationFrame(raf); wsDisconnect(); };
  }, []);
}
