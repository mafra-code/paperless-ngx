import { Injectable } from '@angular/core'
import { Observable } from 'rxjs'
import { Tag } from 'src/app/data/tag'

import { AbstractNameFilterService } from './abstract-name-filter-service'

export interface TagMergeChoice {
  id: number
  name: string
  document_count: number
  is_inbox: boolean
  owner: number | null
}

export interface TagMergeGroup {
  suggested_survivor_id: number
  tags: TagMergeChoice[]
}

@Injectable({
  providedIn: 'root',
})
export class TagService extends AbstractNameFilterService<Tag> {
  constructor() {
    super()
    this.resourceName = 'tags'
  }

  merge(target: number, sources: number[]): Observable<{ result: string }> {
    return this.http.post<{ result: string }>(this.getResourceUrl(null, 'merge'), {
      target,
      sources,
    })
  }

  mergeSuggestions(): Observable<{ groups: TagMergeGroup[] }> {
    return this.http.get<{ groups: TagMergeGroup[] }>(
      this.getResourceUrl(null, 'merge_suggestions'),
    )
  }
}
