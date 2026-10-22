"""Minimal implementations of two Meterless agent engines (reference architectures).

- world_model: WorldStoreService + IngestPipelineService + ContextBuilderService
- hmem:        MemoryStoreService + MemoryRetrievalService

Neither engine calls a model. Any model use goes through the injected generator
in whytrail.generator.
"""
