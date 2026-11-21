import { Router } from "express";
import { ingestHandler } from "../controllers/ingest.controller";

export const ingestRouter = Router();

ingestRouter.post("/ingest", ingestHandler);
