import express, { NextFunction, Request, Response } from "express";
import { ingestRouter } from "./routes/ingest.route";
import { logger } from "./utils/logger";
import { getLeadStats } from "./repositories/lead.repository";
import { getQueueDepth } from "./services/classifier.service";

export function createApp() {
  const app = express();

  app.use(express.json({ limit: "1mb" }));

  app.get("/health", (_req, res) =>
    res.status(200).json({ status: "ok", ollamaQueue: getQueueDepth() })
  );

  // Quick visibility into what's stored in SQLite without needing a DB client.
  app.get("/stats", (_req, res) => res.status(200).json({ leadsByStatus: getLeadStats() }));

  app.use("/", ingestRouter);

  // 404
  app.use((req, res) => {
    res.status(404).json({ error: `No route ${req.method} ${req.path}` });
  });

  // Central error handler
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  app.use((err: Error, _req: Request, res: Response, _next: NextFunction) => {
    logger.error({ err }, "Unhandled error");
    res.status(500).json({ error: "Internal server error" });
  });

  return app;
}
