import { Button, FileButton } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { IconUpload } from "@tabler/icons-react";
import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import {
  getActorQueryKey,
  listActorsQueryKey,
} from "@/client/@tanstack/react-query.gen";
import { extractErrorMessage } from "@/lib/api-error";
import { apiFetch } from "@/lib/api-token";

interface AvatarUploadResponse {
  images_written: number;
  nfos_written: number;
}

async function readError(response: Response): Promise<string> {
  const body: unknown = await response.json().catch(() => null);
  const detail = (body as { detail?: unknown } | null)?.detail;
  return typeof detail === "string" && detail ? detail : `HTTP ${response.status}`;
}

/** 从本地文件直传演员头像; 后端同时写 .actors/ 图并改写关联影片 NFO. */
export function ActorAvatarUploadButton({ actorId }: { actorId: number }) {
  const { t } = useTranslation(["metadata", "common"]);
  const queryClient = useQueryClient();
  const [uploading, setUploading] = useState(false);

  async function upload(file: File | null) {
    if (!file) return;
    setUploading(true);
    try {
      const form = new FormData();
      form.append("file", file);
      // 不要手动设置 Content-Type: 浏览器需要自动带 multipart boundary.
      const response = await apiFetch(`/api/actors/${actorId}/avatar`, {
        method: "POST",
        body: form,
      });
      if (!response.ok) throw new Error(await readError(response));
      const data = (await response.json()) as AvatarUploadResponse;
      notifications.show({
        message: t("actors.uploadAvatar.success", {
          images: data.images_written,
          nfos: data.nfos_written,
        }),
        color: "blue",
      });
      void queryClient.invalidateQueries({
        queryKey: getActorQueryKey({ path: { actor_id: actorId } }),
      });
      void queryClient.invalidateQueries({ queryKey: listActorsQueryKey() });
    } catch (err) {
      notifications.show({
        message: extractErrorMessage(err, t("common:toast.operationFailed")),
        color: "red",
      });
    } finally {
      setUploading(false);
    }
  }

  return (
    <FileButton accept="image/*" onChange={(file) => void upload(file)}>
      {(props) => (
        <Button
          {...props}
          size="xs"
          variant="light"
          leftSection={<IconUpload size={14} />}
          mt="xs"
          fullWidth
          loading={uploading}
          disabled={uploading}
        >
          {t("actors.uploadAvatar.action")}
        </Button>
      )}
    </FileButton>
  );
}
