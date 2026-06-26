// Typed client for the Spark Control Plane API. Same-origin in production;
// proxied to :8080 in dev via vite.config.ts.

export type Role = "head" | "worker";

export interface Node {
  id: number;
  role: Role;
  name: string;
  lan_ip: string;
  qsfp_ip: string;
  qsfp_iface: string;
  ssh_user: string;
  ssh_port: number;
  auth_method: "password" | "key";
  sudo_mode: "nopasswd" | "password";
  hardened: boolean;
  has_ssh_password: boolean;
  has_ssh_key: boolean;
  has_sudo_password: boolean;
  created_at: string;
  updated_at: string;
}

export interface NodeInput {
  role: Role;
  name: string;
  lan_ip: string;
  qsfp_ip: string;
  qsfp_iface?: string;
  ssh_user: string;
  ssh_port?: number;
  auth_method?: "password" | "key";
  ssh_password?: string | null;
  ssh_private_key?: string | null;
  ssh_key_passphrase?: string | null;
  sudo_mode?: "nopasswd" | "password";
  sudo_password?: string | null;
}

export interface ConnectionTest {
  ok: boolean;
  message: string;
  hostname?: string | null;
  sudo_ok?: boolean | null;
  docker_ok?: boolean | null;
  gpu_ok?: boolean | null;
  detail?: string | null;
}

export interface ClusterConfig {
  cluster_name: string;
  vllm_image: string;
  qsfp_netmask: number;
  models_subdir: string;
  hf_cache_subdir: string;
  models_container_path: string;
  hf_cache_container_path: string;
  ray_port: number;
  shm_size: string;
}

export interface Settings {
  has_hf_token: boolean;
  status_poll_seconds: number;
  setup_complete: boolean;
}

export interface ModelSuggestion {
  repo_id: string;
  label: string;
  approx_size_gb?: number | null;
  tool_parser?: string | null;
  note?: string | null;
}

export interface ModelNodeState {
  node_id: number;
  node_role: string;
  node_name: string;
  present: boolean;
  size_bytes?: number | null;
  checksum_ok?: boolean | null;
  status: string;
}

export interface Model {
  id: number;
  repo_id: string;
  name: string;
  tool_parser?: string | null;
  size_bytes?: number | null;
  status: string;
  notes?: string | null;
  node_states: ModelNodeState[];
  created_at: string;
}

export interface Instance {
  id: number;
  name: string;
  model_id: number;
  model_repo_id: string;
  model_name: string;
  topology: "cluster" | "single";
  node_id?: number | null;
  node_role?: string | null;
  port: number;
  tensor_parallel_size: number;
  max_model_len?: number | null;
  gpu_memory_utilization: number;
  max_num_seqs?: number | null;
  dtype?: string | null;
  enable_tool_choice: boolean;
  tool_parser?: string | null;
  extra_args?: string | null;
  has_api_key: boolean;
  autostart: boolean;
  systemd_unit?: string | null;
  status: string;
  last_error?: string | null;
}

export interface InstanceInput {
  name: string;
  model_id: number;
  topology: "cluster" | "single";
  node_id?: number | null;
  port: number;
  tensor_parallel_size?: number | null;
  max_model_len?: number | null;
  gpu_memory_utilization?: number;
  max_num_seqs?: number | null;
  dtype?: string | null;
  enable_tool_choice?: boolean;
  tool_parser?: string | null;
  extra_args?: string | null;
  api_key?: string | null;
  autostart?: boolean;
}

export interface Job {
  id: number;
  type: string;
  title: string;
  status: "pending" | "running" | "success" | "error" | "cancelled";
  node_id?: number | null;
  target?: string | null;
  progress?: number | null;
  exit_code?: number | null;
  summary?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  created_at: string;
}

export interface JobAccepted {
  job_id: number;
  message: string;
}

export interface PhaseInfo {
  phase: string;
  title: string;
}

export interface GpuStatus {
  index: number;
  name?: string | null;
  mem_used_mib?: number | null;
  mem_total_mib?: number | null;
  util_pct?: number | null;
  temp_c?: number | null;
  power_w?: number | null;
}

export interface NodeStatus {
  node_id: number;
  role: string;
  name: string;
  reachable: boolean;
  qsfp_link_ok?: boolean | null;
  docker_ok?: boolean | null;
  ray_container_up?: boolean | null;
  gpus: GpuStatus[];
  mem_budget_used_gib?: number | null;
  mem_budget_total_gib?: number | null;
  detail?: string | null;
}

export interface RayStatus {
  reachable: boolean;
  nodes_total?: number | null;
  nodes_alive?: number | null;
  gpus_total?: number | null;
  detail?: string | null;
}

export interface InstanceRuntimeStatus {
  instance_id: number;
  name: string;
  status: string;
  systemd_active?: boolean | null;
  health_ok?: boolean | null;
  served_model?: string | null;
  endpoint?: string | null;
  detail?: string | null;
}

export interface StatusSnapshot {
  setup_complete: boolean;
  qsfp_ok?: boolean | null;
  ray: RayStatus;
  nodes: NodeStatus[];
  instances: InstanceRuntimeStatus[];
  overcommit_warnings: string[];
  generated_at: string;
}

export interface RepoValidation {
  ok: boolean;
  repo_id?: string;
  size_bytes?: number | null;
  gated?: boolean;
  tool_parser?: string | null;
  error?: string;
}

export interface TeardownRequest {
  stop_instances: boolean;
  stop_ray: boolean;
  remove_network: boolean;
  remove_inter_node_ssh: boolean;
  remove_hosts_entries: boolean;
  delete_models: boolean;
}

class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function j<T>(path: string, opts: RequestInit = {}): Promise<T> {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

export function wsUrl(path: string): string {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}${path}`;
}

export const api = {
  health: () => j<{ status: string; version: string }>("/api/health"),

  // nodes
  listNodes: () => j<Node[]>("/api/nodes"),
  createNode: (n: NodeInput) => j<Node>("/api/nodes", { method: "POST", body: JSON.stringify(n) }),
  updateNode: (id: number, n: Partial<NodeInput>) =>
    j<Node>(`/api/nodes/${id}`, { method: "PATCH", body: JSON.stringify(n) }),
  deleteNode: (id: number) => j<void>(`/api/nodes/${id}`, { method: "DELETE" }),
  testNode: (id: number) => j<ConnectionTest>(`/api/nodes/${id}/test`, { method: "POST" }),
  hardenNode: (id: number) => j<JobAccepted>(`/api/nodes/${id}/harden`, { method: "POST" }),

  // cluster
  getConfig: () => j<ClusterConfig>("/api/cluster/config"),
  updateConfig: (c: Partial<ClusterConfig>) =>
    j<ClusterConfig>("/api/cluster/config", { method: "PATCH", body: JSON.stringify(c) }),
  getSettings: () => j<Settings>("/api/cluster/settings"),
  updateSettings: (s: { hf_token?: string; status_poll_seconds?: number }) =>
    j<Settings>("/api/cluster/settings", { method: "PATCH", body: JSON.stringify(s) }),
  listPhases: () => j<PhaseInfo[]>("/api/cluster/phases"),
  runSetup: (phases?: string[]) =>
    j<JobAccepted>("/api/cluster/setup", { method: "POST", body: JSON.stringify({ phases: phases ?? null }) }),
  teardown: (req: TeardownRequest) =>
    j<JobAccepted>("/api/cluster/teardown", { method: "POST", body: JSON.stringify(req) }),

  // models
  listModels: () => j<Model[]>("/api/models"),
  suggestions: () => j<ModelSuggestion[]>("/api/models/suggestions"),
  validateRepo: (repo_id: string) =>
    j<RepoValidation>("/api/models/validate", { method: "POST", body: JSON.stringify({ repo_id }) }),
  addModel: (repo_id: string, name?: string, tool_parser?: string) =>
    j<Model>("/api/models", { method: "POST", body: JSON.stringify({ repo_id, name, tool_parser }) }),
  downloadModel: (id: number, auto_sync = true) =>
    j<JobAccepted>(`/api/models/${id}/download?auto_sync=${auto_sync}`, { method: "POST" }),
  syncModel: (id: number, target_node_id?: number) =>
    j<JobAccepted>(`/api/models/${id}/sync`, { method: "POST", body: JSON.stringify({ target_node_id: target_node_id ?? null }) }),
  refreshModel: (id: number) => j<Model>(`/api/models/${id}/refresh`, { method: "POST" }),
  deleteModelFiles: (id: number, node_ids: number[] | null, drop_row: boolean) =>
    j<JobAccepted>(`/api/models/${id}/delete`, { method: "POST", body: JSON.stringify({ node_ids, drop_row }) }),
  removeModel: (id: number) => j<void>(`/api/models/${id}`, { method: "DELETE" }),

  // instances
  listInstances: () => j<Instance[]>("/api/instances"),
  createInstance: (i: InstanceInput) =>
    j<Instance>("/api/instances", { method: "POST", body: JSON.stringify(i) }),
  updateInstance: (id: number, i: Partial<InstanceInput>) =>
    j<Instance>(`/api/instances/${id}`, { method: "PATCH", body: JSON.stringify(i) }),
  startInstance: (id: number) => j<JobAccepted>(`/api/instances/${id}/start`, { method: "POST" }),
  stopInstance: (id: number) => j<JobAccepted>(`/api/instances/${id}/stop`, { method: "POST" }),
  deleteInstance: (id: number) => j<JobAccepted>(`/api/instances/${id}`, { method: "DELETE" }),

  // status
  getStatus: () => j<StatusSnapshot>("/api/status"),

  // jobs
  listJobs: (limit = 50) => j<Job[]>(`/api/jobs?limit=${limit}`),
  getJob: (id: number) => j<Job & { logs: any[] }>(`/api/jobs/${id}`),
  cancelJob: (id: number) => j<{ cancelled: boolean }>(`/api/jobs/${id}/cancel`, { method: "POST" }),

  // playground
  playground: (body: { instance_id: number; prompt: string; system?: string; max_tokens?: number; temperature?: number }) =>
    j<{ ok: boolean; content?: string; raw?: any; error?: string }>("/api/playground", {
      method: "POST",
      body: JSON.stringify(body),
    }),
};

export { ApiError };
