import SimulationDashboard from '@/components/simulation-dashboard';

export default function Home() {
  // Legacy overview URLs keep their query string and restore the same cluster task.
  return (
    <SimulationDashboard
      key="cluster_coordination"
      initialView="cluster_coordination"
    />
  );
}
