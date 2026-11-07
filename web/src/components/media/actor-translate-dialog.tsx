import {
  Alert,
  Button,
  Checkbox,
  Group,
  Loader,
  Modal,
  Paper,
  Stack,
  Text,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { IconAlertCircle, IconLanguage } from "@tabler/icons-react";
import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  getActorQueryKey,
  listActorsQueryKey,
} from "@/client/@tanstack/react-query.gen";
import { extractErrorMessage } from "@/lib/api-error";
import { apiFetch } from "@/lib/api-token";

interface TranslateItem {
  field: string;
  source: string;
  translation: string;
}

interface TranslateResult {
  target_language: string;
  source_language: string | null;
  characters: number;
  items: TranslateItem[];
}

interface Props {
  opened: boolean;
  onClose: () => void;
  actorId: number;
}

async function readError(response: Response): Promise<string> {
  const body: unknown = await response.json().catch(() => null);
  const detail = (body as { detail?: unknown } | null)?.detail;
  return typeof detail === "string" && detail ? detail : `HTTP ${response.status}`;
}

/**
 * 演员简介 Lara 翻译: 打开即翻译, 只预览不写库.
 * 用户勾选后才 PATCH /api/actors/{id} 写回 overview, 走 WriteMode.MANUAL 自动锁定字段,
 * 与影片翻译的行为一致 (避免一次点击覆盖原文 / 误耗免费额度).
 */
export function ActorTranslateDialog({ opened, onClose, actorId }: Props) {
  const { t } = useTranslation(["metadata", "common"]);
  const queryClient = useQueryClient();
  const [loading, setLoading] = useState(false);
  const [applying, setApplying] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<TranslateResult | null>(null);
  const [selected, setSelected] = useState<Record<string, boolean>>({});

  const run = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await apiFetch(`/api/actors/${actorId}/translate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ fields: ["overview"] }),
      });
      if (!response.ok) throw new Error(await readError(response));
      const data = (await response.json()) as TranslateResult;
      setResult(data);
      setSelected(Object.fromEntries(data.items.map((item) => [item.field, false])));
    } catch (err) {
      setError(extractErrorMessage(err, t("actors.translateOverview.title")));
    } finally {
      setLoading(false);
    }
  }, [actorId, t]);

  useEffect(() => {
    if (!opened) return;
    setResult(null);
    setSelected({});
    setError(null);
    void run();
  }, [opened, run]);

  const apply = async () => {
    if (result == null) return;
    const patch: Record<string, string> = {};
    for (const item of result.items) {
      if (selected[item.field]) patch[item.field] = item.translation;
    }
    if (Object.keys(patch).length === 0) {
      notifications.show({
        message: t("actors.translateOverview.noSelection"),
        color: "yellow",
      });
      return;
    }
    setApplying(true);
    try {
      const response = await apiFetch(`/api/actors/${actorId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      });
      if (!response.ok) throw new Error(await readError(response));
      notifications.show({
        message: t("actors.translateOverview.success"),
        color: "blue",
      });
      void queryClient.invalidateQueries({
        queryKey: getActorQueryKey({ path: { actor_id: actorId } }),
      });
      void queryClient.invalidateQueries({ queryKey: listActorsQueryKey() });
      onClose();
    } catch (err) {
      notifications.show({
        message: extractErrorMessage(err, t("common:toast.operationFailed")),
        color: "red",
      });
    } finally {
      setApplying(false);
    }
  };

  return (
    <Modal
      opened={opened}
      onClose={onClose}
      title={t("actors.translateOverview.title")}
      size="xl"
      centered
      styles={{ body: { maxHeight: "62vh", overflowY: "auto" } }}
    >
      <Stack gap="md">
        <Text size="xs" c="dimmed">
          {t("actors.translateOverview.hint")}
        </Text>

        {loading && (
          <Group justify="center" py="xl">
            <Loader size="sm" />
          </Group>
        )}

        {error != null && !loading && (
          <Alert icon={<IconAlertCircle size={18} />} color="red" variant="light">
            {error}
          </Alert>
        )}

        {result?.items.map((item) => (
          <Paper key={item.field} withBorder p="sm" radius="md">
            <Group justify="space-between" mb={6}>
              <Text size="xs" fw={600}>
                {t("browse.person.overview")}
              </Text>
              <Checkbox
                size="xs"
                checked={selected[item.field] ?? false}
                label={t("actors.translateOverview.apply")}
                onChange={(event) =>
                  setSelected((prev) => ({
                    ...prev,
                    [item.field]: event.currentTarget.checked,
                  }))
                }
              />
            </Group>
            <Text size="xs" c="dimmed" style={{ whiteSpace: "pre-wrap" }}>
              {t("actors.translateOverview.sourceLabel")}: {item.source}
            </Text>
            <Text size="sm" mt={4} style={{ whiteSpace: "pre-wrap" }}>
              {t("actors.translateOverview.resultLabel")}: {item.translation}
            </Text>
          </Paper>
        ))}

        {result != null && (
          <Group justify="space-between">
            <Text size="xs" c="dimmed">
              {result.target_language} ·{" "}
              {t("actors.translateOverview.characters", { chars: result.characters })}
            </Text>
            <Group gap="xs">
              <Button
                size="xs"
                variant="default"
                leftSection={<IconLanguage size={14} />}
                onClick={() => void run()}
                loading={loading}
              >
                {t("actors.translateOverview.retry")}
              </Button>
              <Button size="xs" onClick={() => void apply()} loading={applying}>
                {t("actors.translateOverview.applyButton")}
              </Button>
            </Group>
          </Group>
        )}

        {!loading && error != null && (
          <Group justify="flex-end">
            <Button size="xs" onClick={() => void run()}>
              {t("actors.translateOverview.start")}
            </Button>
          </Group>
        )}
      </Stack>
    </Modal>
  );
}
