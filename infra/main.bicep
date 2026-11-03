targetScope = 'resourceGroup'

@description('Azure region for the Container Apps environment and storage account.')
param location string = resourceGroup().location

@description('Name of the Container Apps managed environment.')
param environmentName string = 'sp-sync-env'

@description('Name of the Container App.')
param appName string = 'sharepoint-sync'

@description('Container image reference accessible to Azure Container Apps.')
param image string

@description('Optional unique revision suffix to refresh mutable image tags.')
param revisionSuffix string = ''

@description('Maximum source/conversion size in bytes.')
@minValue(1)
param maxUploadBytes int = 104857600

@description('Semicolon-separated dot-prefixed source extensions; empty disables allowlist filtering.')
param supportedSourceFileTypes string = ''

@description('Run sync immediately on application startup and subsequently when the interval is due.')
param autoSync bool = false

@description('Automatic sync interval in seconds; Linux cron checks once per minute.')
@minValue(1)
param syncInterval int = 43200

@description('Semicolon-separated HTTPS SharePoint site URLs.')
param sharePointSiteUrls string = ''

@description('Semicolon-separated additional navigation-only SharePoint site URLs.')
param sharePointSiteUrlsNavigation string = ''

@description('Semicolon-separated individual /SitePages/*.aspx page URLs.')
param sharePointSitePages string = ''

@description('Semicolon-separated SharePoint document library names.')
param sharePointLibraryNames string = 'Shared Documents;Site Pages'

@description('External MarkItDown-compatible API base URL.')
param markItDownApiUrl string

@description('Azure Files share name for generated Markdown and plain-text JSON sync state.')
param outputShareName string = 'sharepoint-output'

@description('Existing SMB storage account in the selected subscription. Empty creates a dedicated account/share.')
param existingStorageAccountName string = ''

@description('Resource group containing the existing account; defaults to the Container App resource group.')
param existingStorageAccountResourceGroup string = resourceGroup().name

@description('Container volume name; existing account/share registrations use a stable derived name.')
param outputVolumeName string = 'sharepoint-output'

@description('Output directory for generated Markdown and sync state, at or below outputMountPath.')
param outputPath string = '/mnt/sharepoint'

@description('Mount point of the file share root; outputPath may be a subdirectory.')
param outputMountPath string = outputPath

@description('Enable navigation export for main sites; explicit navigation-only sites are always exported.')
param includeNavigation bool = true

@description('Navigation locations to export, separated by semicolons.')
param navigationLocations string = 'Auto'

@description('Azure Files share quota in GiB.')
@minValue(1)
@maxValue(5120)
param shareQuotaGiB int = 100

var useExistingStorage = !empty(existingStorageAccountName)
var storageAccountName = useExistingStorage ? existingStorageAccountName : 'st${take(uniqueString(resourceGroup().id), 20)}'
var environmentStorageName = useExistingStorage
  ? '${take(outputVolumeName, 45)}-${uniqueString(storageAccountName, outputShareName)}'
  : outputVolumeName

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = if (!useExistingStorage) {
  name: storageAccountName
  location: location
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
  }
}

resource selectedStorageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
  scope: resourceGroup(useExistingStorage ? existingStorageAccountResourceGroup : resourceGroup().name)
}

resource fileService 'Microsoft.Storage/storageAccounts/fileServices@2023-05-01' = if (!useExistingStorage) {
  parent: storageAccount
  name: 'default'
}

resource outputShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = if (!useExistingStorage) {
  parent: fileService
  name: outputShareName
  properties: {
    shareQuota: shareQuotaGiB
    enabledProtocols: 'SMB'
  }
}

resource managedEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: environmentName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'azure-monitor'
    }
  }
}

resource environmentStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: managedEnvironment
  name: environmentStorageName
  dependsOn: [
    outputShare
  ]
  properties: {
    azureFile: {
      accountName: storageAccountName
      accountKey: selectedStorageAccount.listKeys().keys[0].value
      shareName: outputShareName
      accessMode: 'ReadWrite'
    }
  }
}

resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    managedEnvironmentId: managedEnvironment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: false
        targetPort: 8000
        transport: 'http'
        allowInsecure: false
      }
    }
    template: {
      revisionSuffix: empty(revisionSuffix) ? null : revisionSuffix
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
      containers: [
        {
          name: 'sharepoint-sync'
          image: image
          resources: {
            cpu: json('1.0')
            memory: '2Gi'
          }
          env: [
            {
              name: 'SHAREPOINT_SITE_URLS'
              value: sharePointSiteUrls
            }
            {
              name: 'SHAREPOINT_SITE_URLS_NAVIGATION'
              value: sharePointSiteUrlsNavigation
            }
            {
              name: 'SHAREPOINT_SITE_PAGES'
              value: sharePointSitePages
            }
            {
              name: 'SHAREPOINT_LIBRARY_NAMES'
              value: sharePointLibraryNames
            }
            {
              name: 'SHAREPOINT_INCLUDE_NAVIGATION'
              value: string(includeNavigation)
            }
            {
              name: 'SHAREPOINT_NAVIGATION_LOCATIONS'
              value: navigationLocations
            }
            {
              name: 'MARKITDOWN_API_URL'
              value: markItDownApiUrl
            }
            {
              name: 'OUTPUT_DIR'
              value: outputPath
            }
            {
              name: 'MAX_UPLOAD_BYTES'
              value: string(maxUploadBytes)
            }
            {
              name: 'SUPPORTED_SOURCE_FILE_TYPES'
              value: supportedSourceFileTypes
            }
            {
              name: 'AUTO_SYNC'
              value: string(autoSync)
            }
            {
              name: 'SYNC_INTERVAL'
              value: string(syncInterval)
            }
          ]
          volumeMounts: [
            {
              volumeName: outputVolumeName
              mountPath: outputMountPath
            }
          ]
          probes: [
            {
              type: 'Readiness'
              httpGet: {
                path: '/health'
                port: 8000
                scheme: 'HTTP'
              }
              periodSeconds: 30
              initialDelaySeconds: 10
            }
            {
              type: 'Liveness'
              httpGet: {
                path: '/health'
                port: 8000
                scheme: 'HTTP'
              }
              periodSeconds: 30
              initialDelaySeconds: 20
            }
          ]
        }
      ]
      volumes: [
        {
          name: outputVolumeName
          storageType: 'AzureFile'
          storageName: environmentStorage.name
        }
      ]
    }
  }
}

output appId string = containerApp.id
output managedIdentityPrincipalId string = containerApp.identity.principalId
output internalFqdn string = containerApp.properties.configuration.ingress.fqdn
output outputStorageAccountName string = storageAccountName
