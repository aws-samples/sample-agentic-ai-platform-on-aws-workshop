import { useCollection } from "@cloudscape-design/collection-hooks";
import Alert from "@cloudscape-design/components/alert";
import Badge from "@cloudscape-design/components/badge";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import ContentLayout from "@cloudscape-design/components/content-layout";
import Form from "@cloudscape-design/components/form";
import FormField from "@cloudscape-design/components/form-field";
import Header from "@cloudscape-design/components/header";
import Input from "@cloudscape-design/components/input";
import Link from "@cloudscape-design/components/link";
import Modal from "@cloudscape-design/components/modal";
import Pagination from "@cloudscape-design/components/pagination";
import Select, { type SelectProps } from "@cloudscape-design/components/select";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Table from "@cloudscape-design/components/table";
import TextFilter from "@cloudscape-design/components/text-filter";
import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { useCreateDomain, useDeleteDomain, useDomains, useProjects } from "../../api/hooks";
import type { Domain, Project } from "../../api/types";
import { useMe } from "../../auth/MeContext";
import { ConfirmModal } from "../../components/ConfirmModal";
import { EmptyState } from "../../components/EmptyState";
import { ErrorAlert } from "../../components/ErrorAlert";
import { InfoFormField } from "../../components/InfoPopover";
import { GenericStatus } from "../../components/StatusBadge";
import { useBreadcrumbs } from "../../layout/breadcrumbs";
import { formatErrorContent, useNotifications } from "../../layout/notifications";
import { formatBudgetDuration, formatDateTime, formatUsd } from "../../utils/format";
import { CreateProjectModal, DeleteProjectModal, EditProjectModal } from "./ProjectModals";
import { idError } from "./validation";

export function DomainsProjectsPage() {
  useBreadcrumbs([{ text: "Domains and projects", href: "/domains" }]);
  const me = useMe();
  const navigate = useNavigate();
  const domains = useDomains();
  const projects = useProjects();
  const canWrite = me.can("projects:write");

  const [createDomainOpen, setCreateDomainOpen] = useState(false);
  const [domainToDelete, setDomainToDelete] = useState<Domain | null>(null);
  const [selectedDomains, setSelectedDomains] = useState<Domain[]>([]);
  const [createProjectOpen, setCreateProjectOpen] = useState(false);
  const [projectToEdit, setProjectToEdit] = useState<Project | null>(null);
  const [projectToDelete, setProjectToDelete] = useState<Project | null>(null);
  const [domainFilter, setDomainFilter] = useState<string>("");

  const projectRows = (projects.data ?? []).filter((p) => !domainFilter || p.domainId === domainFilter);
  const collection = useCollection(projectRows, {
    filtering: {
      empty: (
        <EmptyState
          title="No projects"
          description="Projects are created with POST /projects, or by an admin here."
          action={canWrite ? <Button onClick={() => setCreateProjectOpen(true)}>Create project</Button> : undefined}
        />
      ),
      noMatch: <EmptyState title="No matches" description="Change the filter text or the domain." />,
      filteringFunction: (item, text) => {
        const haystack = `${item.projectId} ${item.name} ${item.description} ${item.domainId} ${(item.litellm?.models ?? []).join(" ")}`.toLowerCase();
        return haystack.includes(text.toLowerCase());
      },
    },
    pagination: { pageSize: 15 },
    sorting: { defaultState: { sortingColumn: { sortingField: "projectId" } } },
    selection: { trackBy: "projectId" },
  });
  const selectedProject = collection.collectionProps.selectedItems?.[0] ?? null;
  const domainOptions: SelectProps.Option[] = [
    { value: "", label: "All domains" },
    ...(domains.data ?? []).map((d) => ({ value: d.domainId, label: d.name || d.domainId, description: d.domainId })),
  ];
  const domainName = (domainId: string) => domains.data?.find((d) => d.domainId === domainId)?.name ?? domainId;
  const projectCount = (domainId: string) => (projects.data ?? []).filter((p) => p.domainId === domainId).length;

  return (
    <ContentLayout
      header={
        <Header variant="h1" description="A domain is a business area owning projects; a project is one agent with its LiteLLM team, roles, keys, blueprints and releases.">
          Domains and projects
        </Header>
      }
    >
      <SpaceBetween size="l">
        {domains.error ? <ErrorAlert header="Domains could not be loaded" error={domains.error} onRetry={() => void domains.refetch()} /> : null}
        <Table
          header={
            <Header
              variant="h2"
              counter={domains.data ? `(${domains.data.length})` : undefined}
              actions={
                canWrite ? (
                  <SpaceBetween direction="horizontal" size="xs">
                    <Button
                      disabled={!selectedDomains[0]}
                      onClick={() => setDomainToDelete(selectedDomains[0] ?? null)}
                      data-testid="delete-domain"
                    >
                      Delete
                    </Button>
                    <Button variant="primary" onClick={() => setCreateDomainOpen(true)} data-testid="create-domain">
                      Create domain
                    </Button>
                  </SpaceBetween>
                ) : undefined
              }
            >
              Domains
            </Header>
          }
          items={domains.data ?? []}
          loading={domains.isLoading}
          loadingText="Loading domains"
          trackBy="domainId"
          selectionType={canWrite ? "single" : undefined}
          selectedItems={selectedDomains}
          onSelectionChange={({ detail }) => setSelectedDomains([...detail.selectedItems])}
          ariaLabels={{ selectionGroupLabel: "Domain selection", itemSelectionLabel: (_data, item) => item.domainId }}
          columnDefinitions={[
            { id: "domainId", header: "Domain id", cell: (d) => d.domainId, isRowHeader: true },
            { id: "name", header: "Name", cell: (d) => d.name },
            { id: "description", header: "Description", cell: (d) => d.description || "—" },
            { id: "owner", header: "Owner", cell: (d) => d.owner || "—" },
            { id: "projects", header: "Projects", cell: (d) => projectCount(d.domainId) },
            { id: "created", header: "Created", cell: (d) => `${formatDateTime(d.createdAt)} by ${d.createdBy}` },
          ]}
          empty={
            <EmptyState
              title="No domains"
              description="Domains are created with POST /domains, or by an admin here."
              action={canWrite ? <Button onClick={() => setCreateDomainOpen(true)}>Create domain</Button> : undefined}
            />
          }
        />
        {projects.error ? <ErrorAlert header="Projects could not be loaded" error={projects.error} onRetry={() => void projects.refetch()} /> : null}
        <Table
          {...collection.collectionProps}
          header={
            <Header
              variant="h2"
              counter={projects.data ? `(${collection.filteredItemsCount ?? projectRows.length}/${projects.data.length})` : undefined}
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  <Button disabled={!selectedProject} onClick={() => selectedProject && navigate(`/projects/${selectedProject.projectId}`)}>
                    View details
                  </Button>
                  {canWrite ? (
                    <Button disabled={!selectedProject} onClick={() => setProjectToEdit(selectedProject)}>
                      Edit
                    </Button>
                  ) : null}
                  {canWrite ? (
                    <Button disabled={!selectedProject} onClick={() => setProjectToDelete(selectedProject)}>
                      Delete
                    </Button>
                  ) : null}
                  {canWrite ? (
                    <Button variant="primary" onClick={() => setCreateProjectOpen(true)} data-testid="create-project">
                      Create project
                    </Button>
                  ) : null}
                </SpaceBetween>
              }
            >
              Projects
            </Header>
          }
          filter={
            <SpaceBetween direction="horizontal" size="s">
              <TextFilter {...collection.filterProps} filteringPlaceholder="Find projects" countText={`${collection.filteredItemsCount ?? 0} matches`} />
              <Select
                selectedOption={domainOptions.find((o) => o.value === domainFilter) ?? domainOptions[0]}
                options={domainOptions}
                onChange={({ detail }) => setDomainFilter(detail.selectedOption.value ?? "")}
              />
            </SpaceBetween>
          }
          pagination={<Pagination {...collection.paginationProps} />}
          items={collection.items}
          loading={projects.isLoading}
          loadingText="Loading projects"
          trackBy="projectId"
          selectionType="single"
          ariaLabels={{ selectionGroupLabel: "Project selection", itemSelectionLabel: (_data, item) => item.projectId }}
          columnDefinitions={[
            {
              id: "projectId",
              header: "Project",
              sortingField: "projectId",
              isRowHeader: true,
              cell: (p) => (
                <Link
                  href={`/projects/${p.projectId}`}
                  onFollow={(event) => {
                    event.preventDefault();
                    navigate(`/projects/${p.projectId}`);
                  }}
                >
                  {p.projectId}
                </Link>
              ),
            },
            { id: "name", header: "Name", sortingField: "name", cell: (p) => p.name },
            { id: "domain", header: "Domain", sortingField: "domainId", cell: (p) => domainName(p.domainId) },
            {
              id: "models",
              header: "Models",
              cell: (p) => (
                <SpaceBetween direction="horizontal" size="xxs">
                  {(p.litellm?.models ?? []).map((m) => (
                    <Badge key={m} color="grey">
                      {m}
                    </Badge>
                  ))}
                </SpaceBetween>
              ),
            },
            {
              id: "cap",
              header: "Project cap",
              cell: (p) => (p.litellm ? `${formatUsd(p.litellm.maxBudget)} / ${formatBudgetDuration(p.litellm.budgetDuration)}` : "—"),
            },
            { id: "status", header: "Status", cell: (p) => <GenericStatus status={p.status} /> },
            { id: "created", header: "Created", sortingField: "createdAt", cell: (p) => formatDateTime(p.createdAt) },
          ]}
        />
      </SpaceBetween>

      <CreateDomainModal visible={createDomainOpen} onDismiss={() => setCreateDomainOpen(false)} />
      <DeleteDomainModal
        domain={domainToDelete}
        projectCount={domainToDelete ? projectCount(domainToDelete.domainId) : 0}
        onDismiss={() => setDomainToDelete(null)}
        onDeleted={() => {
          setDomainToDelete(null);
          setSelectedDomains([]);
        }}
      />
      <CreateProjectModal
        visible={createProjectOpen}
        domains={domains.data ?? []}
        defaultDomainId={domainFilter || selectedDomains[0]?.domainId}
        onDismiss={() => setCreateProjectOpen(false)}
        onCreated={(project) => {
          setCreateProjectOpen(false);
          navigate(`/projects/${project.projectId}`);
        }}
      />
      {projectToEdit ? <EditProjectModal project={projectToEdit} visible onDismiss={() => setProjectToEdit(null)} /> : null}
      <DeleteProjectModal
        project={projectToDelete}
        visible={!!projectToDelete}
        onDismiss={() => setProjectToDelete(null)}
        onDeleted={() => {
          setProjectToDelete(null);
          collection.actions.setSelectedItems([]);
        }}
      />
    </ContentLayout>
  );
}

function CreateDomainModal({ visible, onDismiss }: { visible: boolean; onDismiss: () => void }) {
  const create = useCreateDomain();
  const notifications = useNotifications();
  const [domainId, setDomainId] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [owner, setOwner] = useState("");
  const [touched, setTouched] = useState(false);
  const error = idError(domainId, "domainId");
  const close = () => {
    setDomainId("");
    setName("");
    setDescription("");
    setOwner("");
    setTouched(false);
    create.reset();
    onDismiss();
  };
  const submit = () => {
    setTouched(true);
    if (error) {
      return;
    }
    create.mutate(
      { domainId: domainId.trim(), name: name.trim() || domainId.trim(), description: description.trim(), owner: owner.trim() },
      {
        onSuccess: (domain) => {
          notifications.success(`Domain ${domain.domainId} created`);
          close();
        },
      },
    );
  };
  return (
    <Modal
      visible={visible}
      onDismiss={close}
      header="Create domain"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={close} disabled={create.isPending}>
              Cancel
            </Button>
            <Button variant="primary" onClick={submit} loading={create.isPending} data-testid="create-domain-submit">
              Create domain
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <Form errorText={create.error ? formatErrorContent(create.error) : undefined}>
        <SpaceBetween size="m">
          <FormField label="Domain id" constraintText="kebab-case (people-ops)" errorText={touched ? error : undefined}>
            <Input value={domainId} onChange={({ detail }) => setDomainId(detail.value)} autoFocus />
          </FormField>
          <FormField label="Name">
            <Input value={name} onChange={({ detail }) => setName(detail.value)} placeholder="People Operations" />
          </FormField>
          <FormField label="Description">
            <Input value={description} onChange={({ detail }) => setDescription(detail.value)} />
          </FormField>
          <InfoFormField label="Owner" help="Email of the domain owner (informational)">
            <Input value={owner} onChange={({ detail }) => setOwner(detail.value)} placeholder="margaret.chen@anycompany.example" />
          </InfoFormField>
        </SpaceBetween>
      </Form>
    </Modal>
  );
}

function DeleteDomainModal({
  domain,
  projectCount,
  onDismiss,
  onDeleted,
}: {
  domain: Domain | null;
  projectCount: number;
  onDismiss: () => void;
  onDeleted: () => void;
}) {
  const remove = useDeleteDomain();
  const notifications = useNotifications();
  return (
    <ConfirmModal
      visible={!!domain}
      header={`Delete domain ${domain?.domainId ?? ""}`}
      confirmText="Delete"
      loading={remove.isPending}
      error={remove.error}
      disabled={projectCount > 0}
      onDismiss={() => {
        remove.reset();
        onDismiss();
      }}
      onConfirm={() =>
        domain &&
        remove.mutate(domain.domainId, {
          onSuccess: () => {
            notifications.success(`Domain ${domain.domainId} deleted`);
            onDeleted();
          },
        })
      }
    >
      {projectCount > 0 ? (
        <Alert type="warning">This domain still has {projectCount} project(s); the API refuses the deletion until they are deleted.</Alert>
      ) : (
        <Box>The domain record is removed; nothing else is affected.</Box>
      )}
    </ConfirmModal>
  );
}
