targetScope = 'resourceGroup'

@description('Azure region for the Container Apps environment and storage account.')
param location string = resourceGroup().location

@description('Name of the Container Apps managed environment.')
param environmentName string = 'sp-sync-env'

@description('Name of the Container App.')
param appName string = 'sharepoint-sync'

@description('Container image reference accessible to Azure Container Apps.')
param image string

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

@description('Mounted path for generated Markdown and sync state.')
param outputPath string = '/mnt/sharepoint'

@description('Enable navigation export for main sites; explicit navigation-only sites are always exported.')
param includeNavigation bool = true

@description('Navigation locations to export, separated by semicolons.')
param navigationLocations string = 'Auto'

@description('Azure Files share quota in GiB.')
@minValue(1)
@maxValue(5120)
param shareQuotaGiB int = 100

var storageAccountName = 'st${take(uniqueString(resourceGroup().id), 20)}'

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
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

resource fileService 'Microsoft.Storage/storageAccounts/fileServices@2023-05-01' = {
  parent: storageAccount
  name: 'default'
}

resource outputShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
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
  name: 'sharepoint-output'
  properties: {
    azureFile: {
      accountName: storageAccount.name
      accountKey: storageAccount.listKeys().keys[0].value
      shareName: outputShare.name
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
          ]
          volumeMounts: [
            {
              volumeName: 'sharepoint-output'
              mountPath: outputPath
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
          name: 'sharepoint-output'
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
output outputStorageAccountName string = storageAccount.name
