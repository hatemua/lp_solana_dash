import { PoolView } from "@/components/PoolView";

export default async function PoolPage({ params }: { params: Promise<{ address: string }> }) {
  const { address } = await params;
  return <PoolView address={address} />;
}
