import { NgClass, NgTemplateOutlet } from '@angular/common'
import { Component, inject } from '@angular/core'
import { FormsModule, ReactiveFormsModule } from '@angular/forms'
import { RouterModule } from '@angular/router'
import {
  NgbDropdownModule,
  NgbModal,
  NgbPaginationModule,
} from '@ng-bootstrap/ng-bootstrap'
import { NgxBootstrapIconsModule } from 'ngx-bootstrap-icons'
import { first } from 'rxjs'
import { TagEditDialogComponent } from 'src/app/components/common/edit-dialog/tag-edit-dialog/tag-edit-dialog.component'
import { FILTER_HAS_TAGS_ALL } from 'src/app/data/filter-rule-type'
import { Tag } from 'src/app/data/tag'
import { IfPermissionsDirective } from 'src/app/directives/if-permissions.directive'
import { SortableDirective } from 'src/app/directives/sortable.directive'
import { PermissionType } from 'src/app/services/permissions.service'
import {
  TagMergeGroup,
  TagService,
} from 'src/app/services/rest/tag.service'
import { ToastService } from 'src/app/services/toast.service'
import { ManagementListComponent } from '../management-list.component'
import { TagDuplicatesDialogComponent } from './tag-duplicates-dialog.component'
import {
  TagMergeDialogComponent,
  TagMergeOption,
} from './tag-merge-dialog.component'

@Component({
  selector: 'pngx-tag-list',
  templateUrl: './../management-list.component.html',
  styleUrls: ['./../management-list.component.scss'],
  imports: [
    SortableDirective,
    IfPermissionsDirective,
    FormsModule,
    ReactiveFormsModule,
    RouterModule,
    NgClass,
    NgTemplateOutlet,
    NgbDropdownModule,
    NgbPaginationModule,
    NgxBootstrapIconsModule,
  ],
})
export class TagListComponent extends ManagementListComponent<Tag> {
  private readonly mergeModal = inject(NgbModal)
  private readonly mergeToast = inject(ToastService)

  constructor() {
    super()
    this.service = inject(TagService)
    this.editDialogComponent = TagEditDialogComponent
    this.filterRuleType = FILTER_HAS_TAGS_ALL
    this.typeName = $localize`tag`
    this.typeNamePlural = $localize`tags`
    this.permissionType = PermissionType.Tag
    this.showTagTools = true
    this.extraColumns = [
      {
        key: 'color',
        name: $localize`Color`,
        badgeFn: (t: Tag) => ({
          text: t.color,
          textColor: t.text_color,
          backgroundColor: t.color,
        }),
      },
    ]
  }

  getDeleteMessage(object: Tag) {
    return $localize`Do you really want to delete the tag "${object.name}"?`
  }

  private get tagService(): TagService {
    return this.service as TagService
  }

  override openTagMerge(): void {
    const ids = Array.from(this.selectedObjects)
    if (ids.length < 2) {
      return
    }
    this.tagService
      .getFew(ids)
      .pipe(first())
      .subscribe({
        next: (result) => this.openMergeDialog(result.results),
        error: (error) =>
          this.mergeToast.showError($localize`Error loading tags`, error),
      })
  }

  override openTagDuplicates(): void {
    this.tagService
      .mergeSuggestions()
      .pipe(first())
      .subscribe({
        next: (response) => {
          const modal = this.mergeModal.open(TagDuplicatesDialogComponent, {
            backdrop: 'static',
            size: 'lg',
          })
          const dialog = modal.componentInstance as TagDuplicatesDialogComponent
          dialog.groups = response.groups
          dialog.mergeGroup.pipe(first()).subscribe((group: TagMergeGroup) => {
            modal.close()
            this.openMergeDialog(group.tags, group.suggested_survivor_id)
          })
        },
        error: (error) =>
          this.mergeToast.showError(
            $localize`Error loading duplicate tags`,
            error,
          ),
      })
  }

  private openMergeDialog(tags: TagMergeOption[], survivorId?: number): void {
    if (tags.length < 2) {
      return
    }
    const modal = this.mergeModal.open(TagMergeDialogComponent, {
      backdrop: 'static',
    })
    const dialog = modal.componentInstance as TagMergeDialogComponent
    dialog.tags = tags
    dialog.survivorId = survivorId ?? tags[0].id
    modal.result
      .then((result: { target: number; sources: number[] }) => {
        this.tagService
          .merge(result.target, result.sources)
          .pipe(first())
          .subscribe({
            next: () => {
              this.clearSelection()
              this.reloadData()
            },
            error: (error) =>
              this.mergeToast.showError($localize`Error merging tags`, error),
          })
      })
      .catch(() => {})
  }

  override reloadData(extraParams: { [key: string]: any } = null) {
    const params = this.nameFilter?.length
      ? extraParams
      : { ...extraParams, is_root: true }
    super.reloadData(params)
  }

  filterData(data: Tag[]) {
    if (!this.nameFilter?.length) {
      return data.filter((tag) => !tag.parent)
    }

    // When filtering by name, exclude children if their parent is also present
    const availableIds = new Set(data.map((tag) => tag.id))
    return data.filter((tag) => !tag.parent || !availableIds.has(tag.parent))
  }

  protected override getSelectableIDs(tags: Tag[]): number[] {
    const ids: number[] = []
    for (const tag of tags.filter(Boolean)) {
      if (tag.id != null) {
        ids.push(tag.id)
      }
      if (Array.isArray(tag.children) && tag.children.length) {
        ids.push(...this.getSelectableIDs(tag.children))
      }
    }
    return ids
  }
}
