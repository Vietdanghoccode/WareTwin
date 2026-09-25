import type { WarehouseLayout } from "./types";

/**
 * Build the navigation grid from the layout. 0 = walkable, 1 = obstacle, 2 = walkway (walkable but slower).
 * Rules are documented in "Navigation grid generation rules". The Python backend must implement the same rules and compare against the same layout in tests.
 * (Tạo lưới điều hướng từ layout. 0 = đi được, 1 = chướng ngại, 2 = lối đi có giảm tốc. Backend Python phải dùng cùng quy tắc và layout để kiểm thử đối chiếu.)
 */
export function buildNavGrid(layout: WarehouseLayout, floor = 1): { cols: number; rows: number; cells: Uint8Array } {
  const { cols, rows, cell_size: cs } = layout.grid;
  const cells = new Uint8Array(cols * rows);
  // The lift shaft is a physical obstacle on every floor; normal paths go around it.
  // Boarding and alighting use the lift state machine's microMove, not the grid.
  // (Giếng thang là chướng ngại vật trên mọi tầng; lên xuống dùng microMove, không qua lưới.)
  // round-9g: the shaft is W 2.8 x D 3.6. Block x by +/-1.4 and z by +/-1.9
  // to cover the mesh, corner posts, and robot sweep margin. Queue cells,
  // door-axis relay cells, and exit candidates remain outside the blocked area.
  // (Giếng thang rộng 2.8 x sâu 3.6 m; khóa x +/-1.4 và z +/-1.9 để đủ khoảng quét.)
  const blockLifts = () => {
    for (const l of layout.lifts ?? []) {
      const x = l.cell[0] + 0.5, z = l.cell[1] + 0.5;
      const c0 = Math.max(0, Math.floor((x - 1.4) / cs)), c1 = Math.min(cols - 1, Math.ceil((x + 1.4) / cs) - 1);
      const r0 = Math.max(0, Math.floor((z - 1.9) / cs)), r1 = Math.min(rows - 1, Math.ceil((z + 1.9) / cs) - 1);
      for (let r = r0; r <= r1; r++) for (let c = c0; c <= c1; c++) cells[r * cols + c] = 1;
    }
  };
  if (floor !== 1) {
    // Floor 2 mezzanine: outside the footprint is nonexistent floor slab and therefore blocked; remove that floor's racks from the walkable footprint.
    // (Tầng lửng 2: bên ngoài footprint là phần sàn không tồn tại nên bị khóa; trừ các kệ của tầng khỏi vùng đi được.)
    cells.fill(1);
    const fp = layout.floors.find((f) => f.id === floor)?.footprint;
    if (fp) {
      const xs = fp.map((p) => p[0]), zs = fp.map((p) => p[1]);
      const c0 = Math.max(0, Math.floor(Math.min(...xs) / cs)), c1 = Math.min(cols - 1, Math.ceil(Math.max(...xs) / cs) - 1);
      const r0 = Math.max(0, Math.floor(Math.min(...zs) / cs)), r1 = Math.min(rows - 1, Math.ceil(Math.max(...zs) / cs) - 1);
      for (let r = r0; r <= r1; r++) for (let c = c0; c <= c1; c++) cells[r * cols + c] = 0;
    }
    for (const r of layout.racks) if (r.blocks_grid && (r.floor ?? 1) === floor) {
      const x0 = r.position[0], z0 = r.position[2], x1 = x0 + r.size[0], z1 = z0 + r.size[2];
      const c0 = Math.max(0, Math.floor(x0 / cs)), c1 = Math.min(cols - 1, Math.ceil(x1 / cs) - 1);
      const r0 = Math.max(0, Math.floor(z0 / cs)), r1 = Math.min(rows - 1, Math.ceil(z1 / cs) - 1);
      for (let rr = r0; rr <= r1; rr++) for (let cc = c0; cc <= c1; cc++) cells[rr * cols + cc] = 1;
    }
    blockLifts();
    return { cols, rows, cells };
  }
  const fillRect = (x0: number, z0: number, x1: number, z1: number, v: number) => {
    const c0 = Math.max(0, Math.floor(x0 / cs)), c1 = Math.min(cols - 1, Math.ceil(x1 / cs) - 1);
    const r0 = Math.max(0, Math.floor(z0 / cs)), r1 = Math.min(rows - 1, Math.ceil(z1 / cs) - 1);
    for (let r = r0; r <= r1; r++) for (let c = c0; c <= c1; c++) cells[r * cols + c] = v;
  };
  for (const w of layout.walkways) {
    const xs = w.polygon.map((p) => p[0]), zs = w.polygon.map((p) => p[1]);
    fillRect(Math.min(...xs), Math.min(...zs), Math.max(...xs), Math.max(...zs), 2);
  }
  for (const r of layout.racks) if (r.blocks_grid && (r.floor ?? 1) === 1) fillRect(r.position[0], r.position[2], r.position[0] + r.size[0], r.position[2] + r.size[2], 1);
  for (const c of layout.conveyors) if (c.blocks_grid) {
    for (let i = 0; i < c.path.length - 1; i++) {
      const [ax, az] = c.path[i], [bx, bz] = c.path[i + 1], hw = c.width / 2;
      fillRect(Math.min(ax, bx) - hw, Math.min(az, bz) - hw, Math.max(ax, bx) + hw, Math.max(az, bz) + hw, 1);
    }
  }
  for (const ra of layout.restricted_areas) if (!ra.robots_allowed) fillRect(ra.rect[0], ra.rect[1], ra.rect[2], ra.rect[3], 1);
  for (const s of layout.stations) fillRect(s.rect[0], s.rect[1], s.rect[2], s.rect[3], 1);
  // Mezzanine support columns use 0.9 x 0.9 m blocked footprints; paths must go around them. (Cột đỡ tầng lửng có footprint 0.9 x 0.9 m bị khóa; đường đi phải vòng qua.)
  for (const [cx, cz] of layout.columns ?? []) fillRect(cx - 0.45, cz - 0.45, cx + 0.45, cz + 0.45, 1);
  // Block physical obstacles such as structural columns so the grid matches the 3D scene. (Khóa các chướng ngại vật thật như cột kết cấu để lưới khớp với cảnh 3D.)
  for (const o of layout.obstacles ?? []) fillRect(o.rect[0], o.rect[1], o.rect[2], o.rect[3], 1);
  // Charger cabinets are physical; blocking them keeps the passage one cell south of the parking row. (Tủ sạc là vật thể thật; khóa chúng để hành lang nằm cách dãy đỗ xe một ô về phía nam.)
  for (const c of layout.charging_stations) fillRect(c.position[0] - 0.45, c.position[2] - 0.4, c.position[0] + 0.45, c.position[2] + 0.5, 1);
  blockLifts();
  return { cols, rows, cells };
}
