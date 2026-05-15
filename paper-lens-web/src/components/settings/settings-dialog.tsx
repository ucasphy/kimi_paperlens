"use client";

import * as React from "react";
import { Settings, KeyRound, CheckCircle2, AlertCircle, Eye, EyeOff } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";
import { toast } from "sonner";

interface ConfigState {
  kimi_config_path: string;
  has_api_key: boolean;
  api_key_masked: string | null;
  has_oauth: boolean;
  auth_method: string;
}

export function SettingsButton() {
  const [open, setOpen] = React.useState(false);

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger
        render={
          <Button variant="ghost" size="icon" className="h-8 w-8" aria-label="设置">
            <Settings className="h-4 w-4" />
          </Button>
        }
      />
      <SettingsDialogContent onClose={() => setOpen(false)} />
    </Dialog>
  );
}

function SettingsDialogContent({ onClose }: { onClose: () => void }) {
  const [config, setConfig] = React.useState<ConfigState | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [saving, setSaving] = React.useState(false);
  const [apiKey, setApiKey] = React.useState("");
  const [showKey, setShowKey] = React.useState(false);

  const loadConfig = React.useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.getConfig();
      setConfig(data);
    } catch (e) {
      toast.error("加载配置失败", { description: String(e) });
    } finally {
      setLoading(false);
    }
  }, []);

  React.useEffect(() => {
    void loadConfig();
  }, [loadConfig]);

  async function handleSave() {
    const key = apiKey.trim();
    if (!key) {
      toast.error("请输入 API Key");
      return;
    }
    setSaving(true);
    try {
      await api.updateConfig(key);
      toast.success("API Key 已保存");
      setApiKey("");
      await loadConfig();
    } catch (e) {
      toast.error("保存失败", { description: String(e) });
    } finally {
      setSaving(false);
    }
  }

  const isAuthenticated = config && (config.has_api_key || config.has_oauth);

  return (
    <DialogContent className="sm:max-w-md">
      <DialogHeader>
        <DialogTitle className="flex items-center gap-2">
          <Settings className="h-4 w-4" />
          设置
        </DialogTitle>
        <DialogDescription>
          配置 Kimi CLI 的 API Key，以便在不同环境中使用。
        </DialogDescription>
      </DialogHeader>

      <div className="space-y-4 py-2">
        {/* Auth status */}
        <div className="rounded-lg border border-border/60 bg-muted/30 p-3">
          <div className="flex items-center gap-2 text-sm font-medium">
            <KeyRound className="h-4 w-4 text-primary" />
            认证状态
          </div>
          {loading ? (
            <div className="mt-2 text-xs text-muted-foreground">加载中…</div>
          ) : config ? (
            <div className="mt-2 space-y-1.5">
              <div className="flex items-center gap-2 text-xs">
                {isAuthenticated ? (
                  <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500" />
                ) : (
                  <AlertCircle className="h-3.5 w-3.5 text-amber-500" />
                )}
                <span className={isAuthenticated ? "text-emerald-600 dark:text-emerald-400" : "text-amber-600 dark:text-amber-400"}>
                  {isAuthenticated ? "已认证" : "未认证"}
                </span>
                <span className="text-muted-foreground">· {config.auth_method === "api_key" ? "API Key" : config.auth_method === "oauth" ? "OAuth" : "无"}</span>
              </div>
              {config.has_api_key && config.api_key_masked && (
                <div className="text-xs text-muted-foreground">
                  当前 Key: {config.api_key_masked}
                </div>
              )}
              <div className="text-[11px] text-muted-foreground/70 font-mono truncate">
                配置文件: {config.kimi_config_path}
              </div>
            </div>
          ) : (
            <div className="mt-2 text-xs text-muted-foreground">无法加载配置</div>
          )}
        </div>

        {/* API Key input */}
        <div className="space-y-2">
          <label htmlFor="api-key" className="text-xs font-medium">
            设置 API Key
          </label>
          <div className="flex gap-2">
            <div className="relative flex-1">
              <Input
                id="api-key"
                type={showKey ? "text" : "password"}
                placeholder="sk-..."
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                className="pr-9 text-xs"
              />
              <button
                type="button"
                onClick={() => setShowKey((s) => !s)}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                aria-label={showKey ? "隐藏" : "显示"}
              >
                {showKey ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
              </button>
            </div>
            <Button
              size="sm"
              onClick={handleSave}
              disabled={saving || !apiKey.trim()}
              className="shrink-0"
            >
              {saving ? "保存中…" : "保存"}
            </Button>
          </div>
          <p className="text-[11px] text-muted-foreground leading-relaxed">
            从{" "}
            <a
              href="https://platform.moonshot.cn/console/api-keys"
              target="_blank"
              rel="noopener noreferrer"
              className="text-primary hover:underline"
            >
              Moonshot 开放平台
            </a>{" "}
            获取 API Key。设置后会覆盖配置文件中的认证方式。
          </p>
        </div>
      </div>

      <div className="flex justify-end">
        <Button variant="outline" size="sm" onClick={onClose}>
          关闭
        </Button>
      </div>
    </DialogContent>
  );
}
